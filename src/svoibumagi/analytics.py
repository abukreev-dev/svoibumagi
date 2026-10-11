"""Read-only analytical views over confirmed ledger events and report values."""

from collections import defaultdict
from decimal import Decimal


TABS = (
    ("value", "Стоимость портфеля"),
    ("profit", "Прибыль портфеля"),
    ("return", "Доходность"),
    ("benchmark", "Бенчмаркинг"),
    ("instruments", "Эффективность бумаг"),
    ("diversification", "Диверсификация"),
    ("map", "Карта портфеля"),
    ("multiples", "Мультипликаторы"),
    ("cashflow", "Денежный поток"),
)


def build_analytics(estimate: dict, events: list[dict], as_of: str | None,
                    snapshots: list[dict] | None = None) -> dict:
    """Never turn missing market values or incomplete events into zero profit."""
    valued = [row for row in estimate["positions"] if row["value"] is not None and row["value"] > 0]
    total = sum((row["value"] for row in valued), Decimal(0))
    weights = []
    by_instrument: dict[str, dict] = {}
    for row in valued:
        key = row["isin"] or row["name"]
        item = by_instrument.setdefault(key, {"name": row["name"], "isin": row["isin"],
                                              "value": Decimal(0), "quantity": Decimal(0)})
        item["value"] += row["value"]
        item["quantity"] += row["quantity"]
    for item in by_instrument.values():
        item["share"] = item["value"] / total * 100 if total else Decimal(0)
        weights.append(item)
    weights.sort(key=lambda item: item["value"], reverse=True)

    monthly: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    annual: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    kinds = {"deposit": "Пополнения", "withdrawal": "Выводы", "dividend": "Дивиденды",
             "dividend_net": "Дивиденды", "coupon": "Купоны", "tax_withheld": "Налоги",
             "fee": "Комиссии", "trade_fee": "Комиссии"}
    for event in events:
        if event["event_type"] != "cash":
            continue
        payload = event["payload"]
        day = payload.get("date", "")
        if not day or (as_of and day > as_of) or payload.get("currency") != "RUB":
            continue
        label = kinds.get(payload.get("kind"))
        if label:
            amount = Decimal(payload["amount"])
            monthly[day[:7]][label] += amount
            annual[day[:4]][label] += amount
    cash = sum((Decimal(row["end_amount"]) for snapshot in snapshots or []
                for row in snapshot["cash_snapshots"] if row["currency"] == "RUB"), Decimal(0))
    priced_total = estimate["known_securities_subtotal"]
    return {"tabs": TABS, "weights": weights, "priced_total": priced_total,
            "known_cash": cash, "known_total": priced_total + cash,
            "monthly": sorted(monthly.items(), reverse=True),
            "annual": sorted(annual.items(), reverse=True),
            "known_positions": len(valued),
            "unpriced_positions": sum(row["quantity"] != 0 and row["value"] is None
                                      for row in estimate["positions"])}
