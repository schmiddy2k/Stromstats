import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from model import calculate, consumption, fill_from_contract_readings, months_cost, preview_contract, sections, validate_contract


RAW = {"provider": "Beispiel", "tariff": "Fix", "start": "2025-07-01", "end": "2026-06-30", "base_monthly": "12", "price_kwh": "0.3", "contract_kwh": "3000", "bonus1": "120", "bonus2": "0", "bonus3": "0", "entity_id": "sensor.strom_kwh"}


class ModelTest(unittest.TestCase):
    def test_cross_year_split_and_cost(self):
        contract = validate_contract(RAW)
        parts = sections(contract, date(2026, 7, 1))
        self.assertEqual(parts, [(date(2025, 7, 1), date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 7, 1))])
        self.assertEqual(months_cost(date(2025, 7, 1), date(2026, 1, 1), Decimal(12)), Decimal(72))
        first = calculate(contract, *parts[0], Decimal(1000), "Zählerstände")
        second = calculate(contract, *parts[1], Decimal(1200), "Zählerstände")
        self.assertAlmostEqual(first["net"] + second["net"], 144 + 660 - 120)

    def test_partial_month_leap_year(self):
        self.assertEqual(months_cost(date(2024, 2, 15), date(2024, 3, 1), Decimal(29)), Decimal(15))

    def test_live_preview_uses_full_term_and_contract_kwh(self):
        preview = preview_contract({**RAW, "start": "2025-01-01", "end": "2025-12-31", "base_monthly": "10", "price_kwh": "0.30", "contract_kwh": "1000", "bonus1": "50", "bonus2": "25", "bonus3": "0"})
        self.assertEqual(preview, {"base": 120.0, "energy": 300.0, "gross": 420.0, "bonus": 75.0, "effective": 345.0, "effective_per_kwh": 0.345})

    def test_manual_readings_override_gaps(self):
        value, status = consumption(date(2025, 1, 1), date(2026, 1, 1), [], {"2025-01-01": "100", "2026-01-01": "1800"})
        self.assertEqual((value, status), (Decimal(1700), "Zählerstände"))
        self.assertEqual(consumption(date(2025, 1, 1), date(2026, 1, 1), [], {})[0], None)

    def test_partial_statistics_are_labeled(self):
        rows = [{"start": "2025-01-01T01:00:00+01:00", "end": "2025-01-01T02:00:00+01:00", "change": 2}]
        self.assertEqual(consumption(date(2025, 1, 1), date(2025, 1, 2), rows, {})[1], "Statistik (unvollständig)")

    def test_internal_statistics_gap_is_labeled(self):
        rows = [
            {"start": "2025-01-01T00:00:00+01:00", "end": "2025-01-01T01:00:00+01:00", "change": 1},
            {"start": "2025-01-01T23:00:00+01:00", "end": "2025-01-02T00:00:00+01:00", "change": 1},
        ]
        self.assertEqual(consumption(date(2025, 1, 1), date(2025, 1, 2), rows, {})[1], "Statistik (unvollständig)")

    def test_invalid_contract(self):
        with self.assertRaises(ValueError):
            validate_contract({**RAW, "end": "2025-01-01"})

    def test_contract_without_sensor(self):
        self.assertEqual(validate_contract({**RAW, "entity_id": ""})["entity_id"], "")

    def test_completed_contract_estimates_only_missing_years(self):
        contract = validate_contract({**RAW, "start": "2025-01-01", "end": "2026-12-31"})
        periods = sections(contract, date(2027, 1, 1))
        readings = {"2025-01-01": "100", "2027-01-01": "1000"}
        values = [(None, "Keine Messdaten"), (None, "Keine Messdaten")]
        filled = fill_from_contract_readings(contract, periods, values, readings)
        self.assertEqual(sum((v for v, _ in filled), Decimal(0)), Decimal(900))
        self.assertTrue(all(status == "Aus Endständen geschätzt" for _, status in filled))
        exact = fill_from_contract_readings(contract, periods, [(Decimal(400), "Statistik"), values[1]], readings)
        self.assertEqual(exact, [(Decimal(400), "Statistik"), (Decimal(500), "Aus Endständen geschätzt")])

    def test_ongoing_contract_is_not_extrapolated(self):
        contract = validate_contract(RAW)
        periods = sections(contract, date(2025, 12, 31))
        values = [(None, "Keine Messdaten")]
        self.assertEqual(fill_from_contract_readings(contract, periods, values, {"2025-07-01": "10", "2026-07-01": "900"}), values)


if __name__ == "__main__":
    unittest.main()
