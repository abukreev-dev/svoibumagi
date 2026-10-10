"""Broker report contracts. SPDX-License-Identifier: GPL-3.0-or-later"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
from typing import Literal


@dataclass(frozen=True)
class Location:
    sheet: str
    row: int


@dataclass(frozen=True)
class Trade:
    location: Location
    broker_id: str | None
    traded_at: date
    settled_at: date
    isin: str
    name: str
    quantity: Decimal  # signed: positive purchase, negative sale
    price: Decimal
    amount: Decimal
    commission: Decimal
    currency: str
    accrued_interest: Decimal | None = None  # Sber reports NKD, including explicit zero; Alfa uses cash rows.
    execution_time: str | None = None  # HH:MM:SS from the broker row, when present.
    execution_time_required: bool = False


@dataclass(frozen=True)
class CashMovement:
    location: Location
    date: date
    kind: str
    amount: Decimal
    currency: str
    broker_trade_id: str | None = None
    withheld_tax: Decimal | None = None  # Reported within a net dividend transfer comment.
    component: str = ""  # Distinguishes two cash events sourced from one report row.


@dataclass(frozen=True)
class PositionSnapshot:
    location: Location
    as_of: date
    isin: str | None
    name: str
    start_quantity: Decimal
    end_quantity: Decimal
    currency: str = "RUB"


@dataclass(frozen=True)
class CashSnapshot:
    location: Location
    as_of: date
    start_amount: Decimal
    end_amount: Decimal
    currency: str


@dataclass(frozen=True)
class CashFlowSubtotal:
    location: Location
    kind: str  # deposit, withdrawal, dividend, coupon, or bank_fee
    amount: Decimal
    currency: str = "RUB"


@dataclass(frozen=True)
class SourceTotal:
    location: Location
    key: str
    calculated: Decimal
    reported: Decimal


@dataclass(frozen=True)
class UnparsedRow:
    location: Location
    reason: str


@dataclass
class ParsedReport:
    broker: Literal["alfa", "sber"]
    variant: str
    period_start: date
    period_end: date
    account_key: str | None = None  # Private digest of the report's contract identity.
    account_kind: str | None = None
    account_number: str | None = None  # Source number for owner-only review; never use as a matching key.
    trades: list[Trade] = field(default_factory=list)
    summary_trades: list[Trade] = field(default_factory=list)  # Report controls without a stable event ID.
    cash_movements: list[CashMovement] = field(default_factory=list)
    positions: list[PositionSnapshot] = field(default_factory=list)
    cash_snapshots: list[CashSnapshot] = field(default_factory=list)
    cash_flow_subtotals: list[CashFlowSubtotal] = field(default_factory=list)
    source_totals: list[SourceTotal] = field(default_factory=list)
    unparsed: list[UnparsedRow] = field(default_factory=list)
    excluded: list[UnparsedRow] = field(default_factory=list)  # classified rows outside settled ledger
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        # Parsing alone cannot establish accounting correctness.
        return False


def account_key(broker: str, reference: str) -> str:
    normalized = " ".join(reference.strip().upper().split())
    return hashlib.sha256(f"{broker}:{normalized}".encode()).hexdigest()
