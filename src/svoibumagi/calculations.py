"""Independent decimal accounting functions. SPDX-License-Identifier: GPL-3.0-or-later"""

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, localcontext

from .reports import Trade


class IncompleteHistory(ValueError):
    """The available operations cannot prove a financial result."""


@dataclass
class Lot:
    quantity: Decimal
    cost: Decimal
    acquired_at: date | None = None


@dataclass(frozen=True)
class SaleResult:
    broker_id: str | None
    quantity: Decimal
    proceeds: Decimal
    cost: Decimal
    result: Decimal


@dataclass(frozen=True)
class PrincipalReturn:
    cost_reduction: Decimal
    cash_received: Decimal
    realized_result: Decimal


@dataclass(frozen=True)
class PrincipalAction:
    effective_at: date
    isin: str
    principal_fraction: Decimal
    cash_received: Decimal


@dataclass(frozen=True)
class PrincipalResult:
    action: PrincipalAction
    quantity_change: Decimal
    cost_reduction: Decimal
    realized_result: Decimal


@dataclass(frozen=True)
class SecurityTransfer:
    identifier: int
    effective_at: date
    source_account_id: int
    destination_account_id: int
    isin: str
    quantity: Decimal


def transfer_lots(source: dict[str, list[Lot]], destination: dict[str, list[Lot]],
                  isin: str, quantity: Decimal) -> Decimal:
    """Move FIFO lots and original cost basis between owned accounts."""
    if source is destination:
        raise ValueError("Source and destination accounts must differ")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Transfer quantity must be positive")
    if any(not lot.quantity.is_finite() or lot.quantity <= 0 or not lot.cost.is_finite()
           for lot in source.get(isin, []) + destination.get(isin, [])):
        raise IncompleteHistory(f"Security transfer has invalid confirmed lots: {isin}")
    available = sum((lot.quantity for lot in source.get(isin, [])), Decimal(0))
    if available < quantity:
        raise IncompleteHistory(f"Security transfer exceeds confirmed lots: {isin}")
    if any(lot.acquired_at is None for lot in source.get(isin, []) + destination.get(isin, [])):
        raise IncompleteHistory(f"Security transfer has no confirmed acquisition order: {isin}")
    to_move = quantity
    moved_dates = set()
    for lot in source.get(isin, []):
        taken = min(to_move, lot.quantity)
        if taken > 0:
            moved_dates.add(lot.acquired_at)
            to_move -= taken
        if to_move == 0:
            break
    if moved_dates.intersection(lot.acquired_at for lot in destination.get(isin, [])):
        raise IncompleteHistory(f"Security transfer shares an acquisition date with destination lots: {isin}")
    remaining = quantity
    moved: list[Lot] = []
    bucket = source[isin]
    while remaining:
        lot = bucket[0]
        taken = min(remaining, lot.quantity)
        cost = lot.cost if taken == lot.quantity else lot.cost * taken / lot.quantity
        moved.append(Lot(taken, cost, lot.acquired_at))
        lot.quantity -= taken
        lot.cost -= cost
        remaining -= taken
        if lot.quantity == 0:
            bucket.pop(0)
    destination.setdefault(isin, []).extend(moved)
    destination[isin].sort(key=lambda lot: lot.acquired_at)
    return sum((lot.cost for lot in moved), Decimal(0))


def amortize_lots(lots: dict[str, list[Lot]], isin: str, principal_fraction: Decimal,
                  cash_received: Decimal) -> PrincipalReturn:
    """Reduce remaining bond cost pro rata; fraction 1 closes all lots."""
    if (not principal_fraction.is_finite() or not cash_received.is_finite()
            or not Decimal(0) < principal_fraction <= Decimal(1) or cash_received < 0):
        raise ValueError("Invalid amortization fraction or cash amount")
    bucket = lots.get(isin, [])
    if not bucket:
        raise IncompleteHistory(f"Principal return has no confirmed lots: {isin}")
    if any(not lot.quantity.is_finite() or lot.quantity <= 0 or not lot.cost.is_finite()
           for lot in bucket):
        raise IncompleteHistory(f"Principal return has invalid confirmed lots: {isin}")
    cost_reduction = sum((lot.cost * principal_fraction for lot in bucket), Decimal(0))
    for lot in bucket:
        lot.cost -= lot.cost * principal_fraction
    if principal_fraction == 1:
        bucket.clear()
    return PrincipalReturn(cost_reduction, cash_received, cash_received - cost_reduction)


def fifo(trades: list[Trade], opening_lots: dict[str, list[Lot]] | None = None) -> tuple[dict[str, list[Lot]], list[SaleResult]]:
    """FIFO by settlement date; opening lots must have confirmed cost basis."""
    lots = {key: [Lot(lot.quantity, lot.cost, lot.acquired_at) for lot in values] for key, values in (opening_lots or {}).items()}
    if any(not lot.quantity.is_finite() or lot.quantity <= 0 or not lot.cost.is_finite()
           for bucket in lots.values() for lot in bucket):
        raise IncompleteHistory("Opening lots contain invalid amounts")
    sales: list[SaleResult] = []
    groups: dict[tuple[date, date, str], list[Trade]] = {}
    for trade in trades:
        if (any(not value.is_finite() for value in (trade.quantity, trade.price, trade.amount, trade.commission))
                or trade.accrued_interest is not None and not trade.accrued_interest.is_finite()):
            raise ValueError("Trade contains a non-finite amount")
        if trade.execution_time_required and trade.execution_time is None:
            raise IncompleteHistory(f"Trade execution time is unavailable: {trade.isin}")
        if trade.execution_time is not None:
            try:
                time.fromisoformat(trade.execution_time)
            except ValueError as exc:
                raise IncompleteHistory(f"Trade execution time is invalid: {trade.isin}") from exc
        groups.setdefault((trade.settled_at, trade.traded_at, trade.isin), []).append(trade)
    for (_, _, isin), group in groups.items():
        if len(group) > 1 and any(t.execution_time is not None for t in group) and any(
            t.execution_time is None for t in group
        ):
            raise IncompleteHistory(f"Same-day trades have incomplete execution order: {isin}")
        timed: dict[str, set[bool]] = {}
        for trade in group:
            if trade.execution_time is not None:
                timed.setdefault(trade.execution_time, set()).add(trade.quantity > 0)
        if any(len(sides) > 1 for sides in timed.values()):
            raise IncompleteHistory(f"Opposite trades share an execution time: {isin}")
    ordered = sorted(trades, key=lambda t: (t.settled_at, t.traded_at,
                                            time.fromisoformat(t.execution_time) if t.execution_time else time.min,
                                            t.location.sheet, t.location.row, t.broker_id or ""))
    for index, trade in enumerate(ordered):
        if trade.quantity == 0 or trade.amount < 0 or trade.commission < 0:
            raise ValueError("Invalid trade signs")
        bucket = lots.setdefault(trade.isin, [])
        if trade.quantity > 0:
            bucket.append(Lot(trade.quantity, trade.amount + trade.commission, trade.settled_at))
            continue
        remaining = -trade.quantity
        cost = Decimal(0)
        while remaining > 0:
            if not bucket:
                later_same_day_buy = any(
                    later.isin == trade.isin and later.settled_at == trade.settled_at and later.quantity > 0
                    for later in ordered[index + 1:]
                )
                if later_same_day_buy:
                    raise IncompleteHistory(
                        f"Sale precedes available lot on {trade.settled_at}: {trade.isin}; "
                        "same-day later purchase requires short-position accounting or verified execution order"
                    )
                raise IncompleteHistory(f"Sale has no confirmed opening lot: {trade.isin}")
            lot = bucket[0]
            taken = min(remaining, lot.quantity)
            allocated = lot.cost if taken == lot.quantity else lot.cost * taken / lot.quantity
            cost += allocated
            lot.quantity -= taken
            lot.cost -= allocated
            remaining -= taken
            if lot.quantity == 0:
                bucket.pop(0)
        proceeds = trade.amount - trade.commission
        sales.append(SaleResult(trade.broker_id, -trade.quantity, proceeds, cost, proceeds - cost))
    return lots, sales


def fifo_with_principal(trades: list[Trade], actions: list[PrincipalAction]) -> tuple[dict[str, list[Lot]], list[SaleResult], list[PrincipalResult]]:
    """Replay documented principal returns among trades without inventing intraday order."""
    if not actions:
        lots, sales = fifo(trades)
        return lots, sales, []
    ordered = sorted(actions, key=lambda action: (action.effective_at, action.isin))
    if any((left.effective_at, left.isin) == (right.effective_at, right.isin)
           for left, right in zip(ordered, ordered[1:])):
        raise IncompleteHistory("Multiple principal actions on one instrument and date need verified order")
    trade_days = {(trade.settled_at, trade.isin) for trade in trades}
    if any((action.effective_at, action.isin) in trade_days for action in ordered):
        raise IncompleteHistory("Principal action and trade on the same date need verified order")
    remaining = list(trades)
    lots: dict[str, list[Lot]] = {}
    sales: list[SaleResult] = []
    results: list[PrincipalResult] = []
    for action in ordered:
        before = [trade for trade in remaining if trade.settled_at <= action.effective_at]
        remaining = [trade for trade in remaining if trade.settled_at > action.effective_at]
        lots, sold = fifo(before, lots)
        sales.extend(sold)
        quantity = sum((lot.quantity for lot in lots.get(action.isin, [])), Decimal(0))
        returned = amortize_lots(lots, action.isin, action.principal_fraction, action.cash_received)
        results.append(PrincipalResult(action, -quantity if action.principal_fraction == 1 else Decimal(0),
                                       returned.cost_reduction, returned.realized_result))
    lots, sold = fifo(remaining, lots)
    sales.extend(sold)
    return lots, sales, results


def replay_portfolio_lots(
    trades_by_account: dict[int, list[Trade]],
    actions_by_account: dict[int, list[PrincipalAction]],
    transfers: list[SecurityTransfer],
) -> dict[int, tuple[dict[str, list[Lot]], list[SaleResult], list[PrincipalResult]]]:
    """Replay owned accounts together so transferred lots retain date and cost."""
    account_ids = set(trades_by_account) | set(actions_by_account)
    for transfer in transfers:
        account_ids.update((transfer.source_account_id, transfer.destination_account_id))
        if (transfer.source_account_id == transfer.destination_account_id
                or not transfer.quantity.is_finite() or transfer.quantity <= 0):
            raise ValueError("Invalid security transfer accounts or quantity")
    lots = {account_id: {} for account_id in account_ids}
    sales = {account_id: [] for account_id in account_ids}
    principal = {account_id: [] for account_id in account_ids}
    days = {trade.settled_at for values in trades_by_account.values() for trade in values}
    days.update(action.effective_at for values in actions_by_account.values() for action in values)
    days.update(transfer.effective_at for transfer in transfers)
    for day in sorted(days):
        day_trades = {account_id: [trade for trade in values if trade.settled_at == day]
                      for account_id, values in trades_by_account.items()}
        day_actions = {account_id: [action for action in values if action.effective_at == day]
                       for account_id, values in actions_by_account.items()}
        day_transfers = [transfer for transfer in transfers if transfer.effective_at == day]
        for account_id in account_ids:
            traded = {trade.isin for trade in day_trades.get(account_id, [])}
            acted = [action.isin for action in day_actions.get(account_id, [])]
            if traded.intersection(acted) or len(acted) != len(set(acted)):
                raise IncompleteHistory("Trade and principal action on the same instrument and date need verified order")
        seen_transfers = set()
        for transfer in day_transfers:
            if transfer.isin in seen_transfers:
                raise IncompleteHistory("Multiple security transfers on one instrument and date need verified order")
            seen_transfers.add(transfer.isin)
            for account_id in (transfer.source_account_id, transfer.destination_account_id):
                if transfer.isin in {trade.isin for trade in day_trades.get(account_id, [])} or transfer.isin in {
                    action.isin for action in day_actions.get(account_id, [])
                }:
                    raise IncompleteHistory("Security transfer and trade or principal action on the same date need verified order")
        for account_id, trades in day_trades.items():
            if trades:
                lots[account_id], realized = fifo(trades, lots[account_id])
                sales[account_id].extend(realized)
        for account_id, actions in day_actions.items():
            for action in actions:
                bucket = lots[account_id].get(action.isin, [])
                quantity = sum((lot.quantity for lot in bucket), Decimal(0))
                returned = amortize_lots(lots[account_id], action.isin,
                                         action.principal_fraction, action.cash_received)
                principal[account_id].append(PrincipalResult(
                    action, -quantity if action.principal_fraction == 1 else Decimal(0),
                    returned.cost_reduction, returned.realized_result))
        for transfer in day_transfers:
            transfer_lots(lots[transfer.source_account_id], lots[transfer.destination_account_id],
                          transfer.isin, transfer.quantity)
    return {account_id: (lots[account_id], sales[account_id], principal[account_id])
            for account_id in account_ids}


def xirr(flows: list[tuple[date, Decimal]], *, precision: Decimal = Decimal("0.000000001")) -> Decimal:
    """Return annual rate, or reject absent/ambiguous roots and incomplete flows."""
    if precision <= 0 or not precision.is_finite():
        raise ValueError("XIRR precision must be finite and positive")
    if len(flows) < 2 or any(not value.is_finite() for _, value in flows):
        raise IncompleteHistory("XIRR requires dated cash flows of both signs")
    start = min(day for day, _ in flows)
    if max(day for day, _ in flows) == start:
        raise IncompleteHistory("XIRR needs more than one cash-flow date")
    with localcontext() as ctx:
        ctx.prec = 55
        by_day: dict[date, Decimal] = {}
        for day, value in flows:
            by_day[day] = by_day.get(day, Decimal(0)) + value
        terms = [(Decimal((day - start).days) / Decimal(365), value)
                 for day, value in sorted(by_day.items()) if value]
        if len(terms) < 2 or not any(value < 0 for _, value in terms) or not any(value > 0 for _, value in terms):
            raise IncompleteHistory("XIRR requires dated cash flows of both signs")

        # In log(1 + rate) coordinates, NPV is an exponential polynomial.
        # Its derivative has one fewer term after removing a positive factor.
        # Derivative roots partition the domain into monotone intervals, so
        # this finds even pairs of roots hidden between sampled rate factors.
        lower, upper = Decimal("0.000001").ln(), Decimal("1000000").ln()
        tolerance = Decimal("1e-38")

        def evaluate(items: list[tuple[Decimal, Decimal]], point: Decimal) -> tuple[int, Decimal]:
            contributions = [coefficient * (-exponent * point).exp() for exponent, coefficient in items]
            result = sum(contributions, Decimal(0))
            scale = sum((abs(value) for value in contributions), Decimal(0))
            if abs(result) <= scale * tolerance:
                return 0, result
            return (1 if result > 0 else -1), result

        def bisect(items: list[tuple[Decimal, Decimal]], left: Decimal, right: Decimal) -> Decimal:
            left_sign, _ = evaluate(items, left)
            for _ in range(180):
                middle = (left + right) / 2
                middle_sign, _ = evaluate(items, middle)
                if middle_sign == 0 or right - left <= Decimal("1e-40"):
                    return middle
                if middle_sign == left_sign:
                    left = middle
                else:
                    right = middle
            return (left + right) / 2

        def roots(items: list[tuple[Decimal, Decimal]]) -> list[Decimal]:
            offset = items[0][0]
            items = [(exponent - offset, coefficient) for exponent, coefficient in items]
            signs = [coefficient > 0 for _, coefficient in items]
            changes = sum(a != b for a, b in zip(signs, signs[1:]))
            if changes == 0:
                return []
            if changes == 1:
                points = [lower, upper]
            else:
                derivative = [(exponent, -exponent * coefficient)
                              for exponent, coefficient in items[1:]]
                points = [lower, *roots(derivative), upper]
            values = [evaluate(items, point)[0] for point in points]
            # A tiny nonzero extremum can round to zero. Without a sign change
            # we cannot certify a tangent root, so never report it as a rate.
            if 0 in values:
                raise IncompleteHistory("XIRR root uniqueness cannot be established near a stationary point or domain boundary")
            found = []
            for index in range(len(points) - 1):
                if values[index] * values[index + 1] < 0:
                    found.append(bisect(items, points[index], points[index + 1]))
            return sorted(found)

        candidates = roots(terms)
        if len(candidates) != 1:
            raise IncompleteHistory("XIRR has no unique root in supported domain")
        return +(candidates[0].exp() - 1)
