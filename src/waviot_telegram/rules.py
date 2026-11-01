"""Consumption decisions accept verified period usage, never raw impulses."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PeriodUsage:
    amount: Decimal | None
    start: int
    end: int
    verified_complete: bool = False


def classify_consumption(gas, water, electricity):
    def usable(value):
        return value is not None and value.verified_complete and value.amount is not None and value.amount.is_finite() and value.amount >= 0
    if not usable(gas) or gas.end - gas.start < 72 * 3600:
        return "insufficient_data"
    if gas.amount > 0:
        return "normal"
    auxiliary = [value for value in (water, electricity) if usable(value) and (value.start, value.end) == (gas.start, gas.end)]
    if any(value.amount > 0 for value in auxiliary):
        return "gas_flat_with_activity"
    return "no_activity" if len(auxiliary) == 2 else "insufficient_data"
