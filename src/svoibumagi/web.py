"""Private single-owner web workflow. SPDX-License-Identifier: GPL-3.0-or-later"""

import hmac
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from datetime import datetime
from pathlib import Path
import secrets
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.background import BackgroundTask

from .auth import verify_password
from .analytics import TABS, build_analytics
from .backup import create_backup
from .exports import gaps_csv, metrics_csv, operations_csv, positions_csv, rub_text, valuations_csv
from .metrics import evaluate_combined
from .locale_ru import ACCOUNT_KINDS, BROKERS, CASH_KINDS, CHECK_KINDS, EVENT_TYPES, IMPORT_STATES, PRICE_SOURCES, STATUSES, reason_ru
from .prices import find_moex_security, get_moex_bond_price, get_moex_share_close, get_moex_share_quote, resolve_moex_quote
from .storage import MAX_UPLOAD_BYTES, Storage


def rub_ui(value: Decimal) -> str:
    """Format a RUB amount for the Russian interface without changing CSV values."""
    return format(Decimal(rub_text(value)), ",.2f").replace(",", "\u202f").replace(".", ",")


TEMPLATES = Environment(loader=PackageLoader("svoibumagi", "templates"), autoescape=select_autoescape(["html"]))
TEMPLATES.filters["rub"] = rub_text
TEMPLATES.filters["rub_ui"] = rub_ui
TEMPLATES.filters["reason_ru"] = reason_ru
TEMPLATES.filters["broker_ru"] = lambda value: BROKERS.get(value, value)
TEMPLATES.filters["kind_ru"] = lambda value: ACCOUNT_KINDS.get(value, value)
TEMPLATES.filters["import_state_ru"] = lambda value: IMPORT_STATES.get(value, value)
TEMPLATES.filters["status_ru"] = lambda value: STATUSES.get(value, value)
TEMPLATES.filters["event_type_ru"] = lambda value: EVENT_TYPES.get(value, value)
TEMPLATES.filters["cash_kind_ru"] = lambda value: CASH_KINDS.get(value, value)
TEMPLATES.filters["check_kind_ru"] = lambda value: CHECK_KINDS.get(value, value)
TEMPLATES.filters["price_source_ru"] = lambda value: PRICE_SOURCES.get(value, value)


def pending_idle_gaps(metrics: list[dict], confirmations: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Offer only uncovered dates that have no active owner confirmation."""
    return {
        metric["account"]["id"]: [
            gap for gap in metric.get("confirmable_gaps", [])
            if not any(period["revoked_at"] is None
                       and period["start_date"] <= gap["end"]
                       and period["end_date"] >= gap["start"]
                       for period in confirmations.get(metric["account"]["id"], []))
        ]
        for metric in metrics
    }


def create_app(data_dir: Path, password_hash: str, *, secure_cookie: bool = True,
               moex_iss_enabled: bool = False) -> FastAPI:
    if not password_hash.startswith("scrypt:"):
        raise ValueError("A scrypt owner password hash is required")
    storage = Storage(data_dir)
    app = FastAPI(title="Свои бумаги", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def private_response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def render(name: str, **context) -> HTMLResponse:
        context.setdefault("logged_in", name != "login.html")
        response = HTMLResponse(TEMPLATES.get_template(name).render(**context))
        response.headers["Cache-Control"] = "no-store"
        return response

    def session(request: Request) -> str | None:
        return storage.get_session_csrf(request.cookies.get("sb_session"))

    def require(request: Request) -> str:
        csrf = session(request)
        if not csrf:
            raise HTTPException(status_code=401, detail="Authentication required")
        return csrf

    def check_csrf(request: Request, form, token: str) -> None:
        submitted = str(form.get("csrf", ""))
        if not hmac.compare_digest(submitted, token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def require_moex_iss() -> None:
        if not moex_iss_enabled:
            raise HTTPException(status_code=403, detail="MOEX ISS access is disabled by configuration")

    def valuation_date(request: Request) -> date | None:
        supplied = request.query_params.get("as_of")
        if not supplied:
            return None
        try:
            chosen = date.fromisoformat(supplied)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid valuation date") from exc
        if chosen > datetime.now(ZoneInfo("Europe/Moscow")).date():
            raise HTTPException(status_code=400, detail="Valuation date cannot be in the future")
        return chosen

    def home_context(csrf: str, error: str | None = None, as_of: date | None = None) -> dict:
        metrics = storage.financial_metrics(as_of)
        snapshots = storage.latest_snapshots(as_of)
        estimate = storage.portfolio_estimate(as_of)
        cash_rows = [row for snapshot in snapshots for row in snapshot["cash_snapshots"]
                     if row["currency"] == "RUB"]
        known_cash = sum((Decimal(row["end_amount"]) for row in cash_rows), Decimal(0))
        inactive_periods = {metric["account"]["id"]: storage.list_inactive_periods(metric["account"]["id"])
                            for metric in metrics}
        return {"csrf": csrf, "accounts": storage.list_accounts(),
                "deletable_accounts": storage.deletable_account_ids(), "attempts": storage.list_attempts(limit=30),
                "snapshots": snapshots, "estimate": estimate, "known_cash": known_cash,
                "known_total": known_cash + estimate["known_securities_subtotal"],
                "metrics": metrics, "combined": evaluate_combined(metrics),
                "inactive_periods": inactive_periods,
                "pending_idle_gaps": pending_idle_gaps(metrics, inactive_periods),
                "event_count": len(storage.list_events()), "error": error,
                "moex_iss_enabled": moex_iss_enabled,
                "today": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if session(request):
            return RedirectResponse("/", status_code=303)
        login_csrf = secrets.token_urlsafe(24)
        response = render("login.html", csrf=login_csrf, error=None)
        response.set_cookie("sb_login_csrf", login_csrf, httponly=True, secure=secure_cookie, samesite="strict", max_age=600)
        return response

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        cookie_csrf = request.cookies.get("sb_login_csrf", "")
        if not cookie_csrf or not hmac.compare_digest(cookie_csrf, str(form.get("csrf", ""))):
            raise HTTPException(status_code=403, detail="Invalid login form")
        if not verify_password(str(form.get("password", "")), password_hash):
            return render("login.html", csrf=cookie_csrf, error="Неверный пароль")
        token, _ = storage.create_session()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("sb_session", token, httponly=True, secure=secure_cookie, samesite="strict", max_age=12 * 3600)
        response.delete_cookie("sb_login_csrf")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        token = require(request)
        form = await request.form()
        check_csrf(request, form, token)
        storage.delete_session(request.cookies.get("sb_session"))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("sb_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        csrf = session(request)
        if not csrf:
            return render("landing.html", logged_in=False)
        return render("home.html", **home_context(csrf, as_of=valuation_date(request)))

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request):
        csrf = require(request)
        view = request.query_params.get("view", "value")
        if view not in {key for key, _ in TABS}:
            raise HTTPException(status_code=404, detail="Неизвестный раздел аналитики")
        as_of = valuation_date(request)
        estimate = storage.portfolio_estimate(as_of)
        metrics = storage.financial_metrics(as_of)
        combined = evaluate_combined(metrics)
        data = build_analytics(estimate, storage.list_events(), estimate["as_of"],
                               storage.latest_snapshots(as_of))
        return render("analytics.html", csrf=csrf, view=view, estimate=estimate,
                      metrics=metrics, combined=combined, data=data)

    @app.post("/accounts")
    async def add_account(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.create_account(str(form.get("broker", "")), str(form.get("kind", "")), str(form.get("name", "")))
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    async def delete_empty_account(request: Request, account_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.delete_empty_account(account_id)
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/history")
    async def confirm_history(request: Request, account_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.confirm_account_history(account_id, date.fromisoformat(str(form.get("from_date", ""))),
                                            str(form.get("reason", "")))
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/inactive")
    async def confirm_inactive(request: Request, account_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.confirm_inactive_period(account_id,
                                            date.fromisoformat(str(form.get("start_date", ""))),
                                            date.fromisoformat(str(form.get("end_date", ""))),
                                            str(form.get("reason", "")))
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/inactive/{period_id}/revoke")
    async def revoke_inactive(request: Request, account_id: int, period_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.revoke_inactive_period(account_id, period_id)
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/imports")
    async def upload(request: Request):
        csrf = require(request)
        body_limit = MAX_UPLOAD_BYTES + 64 * 1024  # Multipart headers and the two small form fields.
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > body_limit:
                    raise HTTPException(status_code=413, detail="Файл превышает лимит")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Некорректный размер запроса") from exc

        received = 0

        async def limited_receive():
            nonlocal received
            message = await request.receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise HTTPException(status_code=413, detail="Файл превышает лимит")
            return message

        bounded_request = Request(request.scope, receive=limited_receive)
        try:
            async with bounded_request.form(max_files=1, max_fields=4, max_part_size=4096) as form:
                check_csrf(request, form, csrf)
                account_id = int(str(form.get("account_id", "")))
                uploaded = form["report"]
                filename = getattr(uploaded, "filename", None)
                if not filename or not hasattr(uploaded, "read"):
                    raise ValueError("Файл не выбран")
                fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=storage.root)
                os.chmod(temp_name, 0o600)
                try:
                    total = 0
                    with os.fdopen(fd, "wb") as output:
                        while chunk := await uploaded.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise HTTPException(status_code=413, detail="Файл превышает лимит")
                            output.write(chunk)
                    attempt_id, _ = storage.stage_file(account_id, Path(temp_name), original_name=filename)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
        except (ValueError, KeyError) as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse(f"/imports/{attempt_id}", status_code=303)

    @app.get("/imports", response_class=HTMLResponse)
    async def import_history(request: Request, page: int = 1, account_id: int | None = None):
        csrf = require(request)
        if page < 1:
            raise HTTPException(status_code=400, detail="Invalid import history page")
        if account_id is not None and account_id not in {item["id"] for item in storage.list_accounts()}:
            raise HTTPException(status_code=400, detail="Invalid import history account")
        page_size = 50
        total = storage.count_attempts(account_id)
        if page > max(1, (total + page_size - 1) // page_size):
            raise HTTPException(status_code=404, detail="Import history page not found")
        attempts = storage.list_attempts(account_id, limit=page_size, offset=(page - 1) * page_size)
        return render("import_history.html", csrf=csrf, attempts=attempts, accounts=storage.list_accounts(),
                      account_id=account_id, page=page, page_size=page_size, total=total)

    @app.post("/prices")
    async def manual_price(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.add_price(str(form.get("isin", "")).strip().upper(), Decimal(str(form.get("amount", ""))),
                              date.fromisoformat(str(form.get("as_of", ""))), note=str(form.get("note", "")))
        except (ValueError, InvalidOperation) as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse("/", status_code=303)

    @app.post("/prices/moex")
    async def moex_share_price(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        require_moex_iss()
        requested = str(form.get("as_of", "")).strip()
        try:
            valuation_day = date.fromisoformat(requested) if requested else None
            quote = (get_moex_share_close(str(form.get("isin", "")), str(form.get("secid", "")),
                                          valuation_day) if valuation_day else
                     get_moex_share_quote(str(form.get("isin", "")), str(form.get("secid", ""))))
            storage.add_price(quote.isin, quote.amount, quote.as_of, source="moex",
                              note=f"TQBR {quote.secid}; {'CLOSE' if valuation_day else 'LAST'}; {quote.url}")
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse(f"/?as_of={valuation_day.isoformat()}" if valuation_day else "/", status_code=303)

    @app.post("/prices/moex/bond")
    async def moex_bond_price(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        require_moex_iss()
        try:
            valuation_day = date.fromisoformat(str(form.get("as_of", "")))
            quote = get_moex_bond_price(str(form.get("isin", "")), str(form.get("secid", "")),
                                        str(form.get("board", "")), valuation_day)
            storage.add_price(quote.isin, quote.amount, quote.as_of, source="moex",
                              note=f"{str(form.get('board', '')).strip().upper()} {quote.secid}; dirty RUB price = historical CLOSE percent * FACEVALUE / 100 + ACCINT; {quote.url}")
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse(f"/?as_of={valuation_day.isoformat()}", status_code=303)

    @app.post("/prices/moex/auto")
    async def moex_auto_price(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        require_moex_iss()
        try:
            valuation_day = date.fromisoformat(str(form.get("as_of", "")))
            security = find_moex_security(str(form.get("isin", "")))
            quote = (get_moex_share_close(security.isin, security.secid, valuation_day)
                     if security.kind == "share" else
                     get_moex_bond_price(security.isin, security.secid, security.board, valuation_day))
            method = "CLOSE RUB" if security.kind == "share" else "dirty RUB = CLOSE percent * FACEVALUE / 100 + ACCINT"
            storage.add_price(quote.isin, quote.amount, quote.as_of, source="moex",
                              note=f"{security.board} {security.secid}; {method}; {quote.url}")
        except ValueError as exc:
            return render("home.html", **home_context(csrf, str(exc)))
        return RedirectResponse(f"/?as_of={valuation_day.isoformat()}", status_code=303)

    @app.post("/prices/moex/refresh")
    async def refresh_position_prices(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        require_moex_iss()
        try:
            valuation_day = date.fromisoformat(str(form.get("as_of", "")))
            if valuation_day > datetime.now(ZoneInfo("Europe/Moscow")).date():
                raise ValueError("Дата оценки не может быть в будущем")
            positions = [row for row in storage.portfolio_estimate(valuation_day)["positions"]
                         if row["quantity"] != 0]
            if not positions:
                raise ValueError("На выбранную дату нет ненулевых позиций")
            if any(not row["isin"] for row in positions):
                raise ValueError("У ненулевой позиции отсутствует ISIN")
            isins = sorted({row["isin"] for row in positions})
            if len(isins) > 100:
                raise ValueError("За один запрос можно обновить до 100 бумаг")

            def fetch_all():
                with ThreadPoolExecutor(max_workers=4) as pool:
                    return list(pool.map(lambda isin: resolve_moex_quote(isin, valuation_day), isins))

            quotes = await asyncio.to_thread(fetch_all)
            storage.add_moex_prices_batch(quotes)
        except ValueError as exc:
            selected = valuation_day if "valuation_day" in locals() else None
            return render("home.html", **home_context(csrf, str(exc), selected))
        return RedirectResponse(f"/?as_of={valuation_day.isoformat()}", status_code=303)

    @app.get("/imports/{attempt_id}", response_class=HTMLResponse)
    async def review(request: Request, attempt_id: int):
        csrf = require(request)
        try:
            result = storage.review_attempt(attempt_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Import not found")
        return render("review.html", csrf=csrf, attempt_id=attempt_id, review=result, error=None)

    @app.post("/imports/{attempt_id}/confirm")
    async def confirm(request: Request, attempt_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            review_data = storage.review_attempt(attempt_id)
            decisions = {}
            for key in review_data["required_decisions"]:
                decisions[key] = {"action": "acknowledge", "reason": str(form.get(f"reason::{key}", ""))}
            for trade in review_data["preview"].get("summary_trades", []):
                location = trade["location"]
                key = f"summary:{location['sheet']}:{location['row']}"
                decisions[key]["action"] = str(form.get(f"action::{key}", ""))
            for item in review_data["ambiguous_events"]:
                key = item["key"]
                selected = str(form.get(f"existing::{key}", "")).strip()
                decisions[key] = {"action": str(form.get(f"action::{key}", "")),
                                  "reason": str(form.get(f"reason::{key}", "")),
                                  "existing_event_id": int(selected) if selected else None}
            storage.confirm_attempt(attempt_id, decisions)
        except ValueError as exc:
            return render("review.html", csrf=csrf, attempt_id=attempt_id, review=storage.review_attempt(attempt_id), error=str(exc))
        return RedirectResponse("/", status_code=303)

    @app.post("/imports/{attempt_id}/reject")
    async def reject(request: Request, attempt_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.reject_attempt(attempt_id, str(form.get("reason", "")))
        except ValueError as exc:
            return render("review.html", csrf=csrf, attempt_id=attempt_id, review=storage.review_attempt(attempt_id), error=str(exc))
        return RedirectResponse("/", status_code=303)

    @app.post("/imports/{attempt_id}/delete")
    async def delete_unconfirmed_import(request: Request, attempt_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.delete_unconfirmed_attempt(attempt_id)
        except ValueError as exc:
            try:
                review_data = storage.review_attempt(attempt_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="Import not found") from exc
            return render("review.html", csrf=csrf, attempt_id=attempt_id, review=review_data, error=str(exc))
        return RedirectResponse("/", status_code=303)

    @app.get("/operations", response_class=HTMLResponse)
    async def operations(request: Request, account_id: int | None = None, event_type: str = "",
                         source_type: str = "", from_date: str = "", to_date: str = "", q: str = ""):
        csrf = require(request)
        if event_type not in {"", "trade", "cash", "security_transfer"} or source_type not in {"", "broker", "manual"}:
            raise HTTPException(status_code=400, detail="Invalid operation filter")
        try:
            start = date.fromisoformat(from_date) if from_date else None
            end = date.fromisoformat(to_date) if to_date else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid operation date") from exc
        if start and end and start > end:
            raise HTTPException(status_code=400, detail="Operation date range is reversed")
        query = q.strip()
        if len(query) > 100:
            raise HTTPException(status_code=400, detail="Operation search is too long")
        needle = query.casefold()
        events = []
        for event in storage.list_events(account_id):
            payload = event["payload"]
            event_date = payload.get("traded_at") or payload.get("date")
            if event_type and event["event_type"] != event_type:
                continue
            if source_type and event["source_type"] != source_type:
                continue
            if (start and event_date < start.isoformat()) or (end and event_date > end.isoformat()):
                continue
            if needle and not any(needle in str(payload.get(field) or "").casefold()
                                  for field in ("name", "isin", "broker_id", "kind")):
                continue
            events.append(event)
        events.sort(key=lambda item: (item["payload"].get("traded_at") or item["payload"].get("date"),
                                      item["created_at"], str(item["id"])), reverse=True)
        return render("operations.html", csrf=csrf, events=events, accounts=storage.list_accounts(), error=None,
                      filters={"account_id": account_id, "event_type": event_type, "source_type": source_type,
                               "from_date": from_date, "to_date": to_date, "q": query})

    @app.get("/instruments", response_class=HTMLResponse)
    async def instruments(request: Request):
        csrf = require(request)
        return render("instruments.html", csrf=csrf, instruments=storage.list_instruments())

    @app.post("/operations/manual/cash")
    async def manual_cash(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.add_manual_cash(int(str(form.get("account_id", ""))), date.fromisoformat(str(form.get("date", ""))),
                                    str(form.get("kind", "")), Decimal(str(form.get("amount", ""))),
                                    str(form.get("reason", "")))
        except (ValueError, InvalidOperation) as exc:
            return render("operations.html", csrf=csrf, events=storage.list_events(), accounts=storage.list_accounts(), error=str(exc))
        return RedirectResponse("/operations", status_code=303)

    @app.post("/operations/manual/trade")
    async def manual_trade(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.add_manual_trade(
                int(str(form.get("account_id", ""))), date.fromisoformat(str(form.get("traded_at", ""))),
                date.fromisoformat(str(form.get("settled_at", ""))), str(form.get("isin", "")).strip().upper(),
                str(form.get("name", "")), Decimal(str(form.get("quantity", ""))),
                Decimal(str(form.get("price", ""))), Decimal(str(form.get("amount", ""))),
                Decimal(str(form.get("commission", ""))), str(form.get("reason", "")),
            )
        except (ValueError, InvalidOperation) as exc:
            return render("operations.html", csrf=csrf, events=storage.list_events(), accounts=storage.list_accounts(), error=str(exc))
        return RedirectResponse("/operations", status_code=303)

    @app.post("/operations/manual/principal")
    async def manual_principal(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.add_manual_principal_action(
                int(str(form.get("account_id", ""))), date.fromisoformat(str(form.get("date", ""))),
                str(form.get("isin", "")).strip().upper(), str(form.get("name", "")),
                Decimal(str(form.get("principal_fraction", ""))), str(form.get("cash_event_id", "")),
                str(form.get("reason", "")),
            )
        except (ValueError, InvalidOperation) as exc:
            return render("operations.html", csrf=csrf, events=storage.list_events(), accounts=storage.list_accounts(), error=str(exc))
        return RedirectResponse("/operations", status_code=303)

    @app.post("/operations/manual/security-transfer")
    async def manual_security_transfer(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            storage.add_security_transfer(
                int(str(form.get("source_account_id", ""))),
                int(str(form.get("destination_account_id", ""))),
                date.fromisoformat(str(form.get("date", ""))),
                str(form.get("isin", "")).strip().upper(), str(form.get("name", "")),
                Decimal(str(form.get("quantity", ""))), str(form.get("reason", "")),
            )
        except (ValueError, InvalidOperation) as exc:
            return render("operations.html", csrf=csrf, events=storage.list_events(),
                          accounts=storage.list_accounts(), error=str(exc))
        return RedirectResponse("/operations", status_code=303)

    @app.post("/operations/{event_id}/classify")
    async def classify_transfer(request: Request, event_id: int):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        try:
            pair = str(form.get("paired_event_id", "")).strip()
            storage.classify_cash_event(event_id, str(form.get("kind", "")), str(form.get("reason", "")), int(pair) if pair else None)
        except ValueError as exc:
            return render("operations.html", csrf=csrf, events=storage.list_events(), accounts=storage.list_accounts(), error=str(exc))
        return RedirectResponse("/operations", status_code=303)

    @app.get("/operations/{event_id}/source")
    async def source(request: Request, event_id: int):
        require(request)
        try:
            info = storage.event_source(event_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Stored source failed integrity check") from exc
        if not info or not info[0].is_file():
            raise HTTPException(status_code=404, detail="Source not found")
        response = FileResponse(info[0], media_type="application/octet-stream", filename=info[1])
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/export/operations.csv")
    async def export_operations(request: Request):
        require(request)
        numbers = {account["id"]: account["account_number"] for account in storage.list_accounts()}
        response = Response(operations_csv(storage.list_events(), numbers), media_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="operations.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/export/positions.csv")
    async def export_positions(request: Request):
        require(request)
        response = Response(positions_csv(storage.latest_snapshots()), media_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="positions.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/export/valuations.csv")
    async def export_valuations(request: Request):
        require(request)
        response = Response(valuations_csv(storage.portfolio_estimate(valuation_date(request))), media_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="valuations.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/export/metrics.csv")
    async def export_metrics(request: Request):
        require(request)
        metrics = storage.financial_metrics(valuation_date(request))
        response = Response(metrics_csv(metrics, evaluate_combined(metrics)), media_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="metrics.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/export/gaps.csv")
    async def export_gaps(request: Request):
        require(request)
        metrics = storage.financial_metrics()
        confirmations = {metric["account"]["id"]: storage.list_inactive_periods(metric["account"]["id"])
                         for metric in metrics}
        response = Response(gaps_csv(metrics, confirmations), media_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="gaps.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/backup")
    async def download_backup(request: Request):
        csrf = require(request)
        form = await request.form()
        check_csrf(request, form, csrf)
        fd, filename = tempfile.mkstemp(prefix="backup-", suffix=".zip", dir=storage.root)
        os.close(fd)
        Path(filename).unlink()
        try:
            create_backup(storage, Path(filename))
        except Exception:
            Path(filename).unlink(missing_ok=True)
            raise
        response = FileResponse(filename, filename="svoibumagi-backup.zip", media_type="application/zip",
                                background=BackgroundTask(Path(filename).unlink, missing_ok=True))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app
