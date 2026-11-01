from decimal import Decimal

from waviot_telegram.rules import PeriodUsage, classify_consumption


def test_only_verified_full_period_consumption_can_trigger():
    end = 1000000
    gas = PeriodUsage(Decimal("0"), end - 259200, end, True)
    water = PeriodUsage(Decimal("0.05"), end - 259200, end, True)
    assert classify_consumption(gas, water, None) == "gas_flat_with_activity"
    assert classify_consumption(PeriodUsage(Decimal("0"), end-259200, end, False), water, None) == "insufficient_data"
    assert classify_consumption(gas, None, None) == "insufficient_data"
    assert classify_consumption(PeriodUsage(Decimal("0"), end-255600, end, True), water, None) == "insufficient_data"
    assert classify_consumption(gas, PeriodUsage(Decimal("0"), end-259200, end, True), None) == "insufficient_data"
