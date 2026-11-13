import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import history


class HistoryTest(unittest.TestCase):
    def _plan(self, missing=False, priced_hours=False):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=48)
        meter_rows = [{"start": (start + timedelta(hours=n)).isoformat(), "change": 1}
                      for n in range(48 if not missing else 47)]
        cost_rows = ([{"start": int((start + timedelta(hours=n)).timestamp() * 1000),
                       "sum": (n + 1) * 0.1, "change": 0.1, "state": (n + 1) * 0.1}
                      for n in range(48)] if priced_hours else [])
        cost_rows.append({"start": int(end.timestamp() * 1000),
                          "sum": 4.8 if priced_hours else 0, "change": 0, "state": 4.8 if priced_hours else 0})

        def ws(command, timeout=15):
            if command["type"] == "energy/get_prefs":
                return {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.meter",
                                            "entity_energy_price": "sensor.stromvertrag_arbeitspreis", "stat_cost": None}]}
            if command["type"] == "energy/info":
                return {"cost_sensors": {"sensor.meter": "sensor.meter_cost"}}
            if command["type"] == "recorder/get_statistics_metadata":
                return [{"statistic_id": "sensor.meter_cost", "source": "recorder",
                         "statistics_unit_of_measurement": "EUR", "mean_type": 0,
                         "has_sum": True, "unit_class": None, "name": None}]
            raise AssertionError(command)

        def rows(statistic_id, *_args):
            return cost_rows

        def ha(_method, path, _body=None):
            if path == "/config":
                return {"currency": "EUR", "time_zone": "UTC"}
            if path.startswith("/services/recorder/get_statistics"):
                return {"service_response": {"statistics": {"sensor.meter": meter_rows}}}
            raise AssertionError(path)

        contract = {"id": 1, "start": "2026-01-01", "end": "2026-12-31",
                    "entity_id": "sensor.meter", "price_kwh": "0.30"}
        with patch.object(history, "websocket_command", side_effect=ws), patch.object(history, "_rows", side_effect=rows):
            return history.build_plan([contract], ha, date(2026, 1, 3))

    def test_preview_calculates_past_hours_without_writing(self):
        plan = self._plan()
        self.assertEqual(plan["hours"], 48)
        self.assertAlmostEqual(plan["calculated_cost"], 14.4)
        self.assertAlmostEqual(plan["adjustment"], 14.4)
        self.assertNotIn("rows", history.public_preview(plan))

    def test_missing_hour_is_skipped(self):
        plan = self._plan(missing=True)
        self.assertEqual(plan["hours"], 47)
        self.assertEqual(plan["skipped_hours"], 1)
        self.assertAlmostEqual(plan["calculated_cost"], 14.1)
        self.assertEqual(plan["contracts"][0]["hours"], 47)

    def test_missing_meter_hour_keeps_existing_cost(self):
        plan = self._plan(missing=True, priced_hours=True)
        self.assertAlmostEqual(plan["existing_cost"], 4.7)
        self.assertAlmostEqual(plan["adjustment"], 14.1 - 4.7)
        self.assertAlmostEqual(plan["rows"][-1]["sum"], 4.8 + plan["adjustment"])

    def test_no_usable_meter_hours_blocks_backfill(self):
        with patch.object(history, "_meter_rows", return_value=[]):
            with self.assertRaisesRegex(ValueError, "keine auswertbaren"):
                self._plan()

    def test_existing_costs_are_replaced_hour_by_hour(self):
        plan = self._plan(priced_hours=True)
        self.assertAlmostEqual(plan["existing_cost"], 4.8)
        self.assertAlmostEqual(plan["adjustment"], 9.6)
        self.assertAlmostEqual(plan["rows"][-1]["sum"], 14.4)

    def test_each_started_contract_uses_its_own_dates_and_price(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        contracts = [
            {"id": 1, "start": "2026-01-01", "end": "2026-01-01", "entity_id": "sensor.meter", "price_kwh": "0.30"},
            {"id": 2, "start": "2026-01-03", "end": "2026-01-04", "entity_id": "sensor.meter", "price_kwh": "0.50"},
            {"id": 3, "start": "2026-01-05", "end": "2026-01-10", "entity_id": "sensor.meter", "price_kwh": "0.90"},
        ]
        calls = []
        def ha(_method, path, body=None):
            if path == "/config":
                return {"currency": "EUR", "time_zone": "UTC"}
            calls.append((body["start_time"], body["end_time"]))
            start = datetime.fromisoformat(body["start_time"])
            rows = [{"start": (start + timedelta(hours=n)).isoformat(), "change": 1}
                    for n in range(24)]
            return {"service_response": {"statistics": {"sensor.meter": rows}}}
        def ws(command, timeout=15):
            if command["type"] == "energy/get_prefs":
                return {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.meter",
                                            "entity_energy_price": "sensor.stromvertrag_arbeitspreis"}]}
            if command["type"] == "energy/info":
                return {"cost_sensors": {"sensor.meter": "sensor.cost"}}
            if command["type"] == "recorder/get_statistics_metadata":
                return [{"source": "recorder", "statistics_unit_of_measurement": "EUR"}]
            raise AssertionError(command)
        future = {"start": int((base + timedelta(days=5)).timestamp() * 1000), "sum": 0, "change": 0, "state": 0}
        with patch.object(history, "websocket_command", side_effect=ws), patch.object(history, "_rows", return_value=[future]):
            plan = history.build_plan(contracts, ha, date(2026, 1, 5))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], (base.isoformat(), (base + timedelta(days=1)).isoformat()))
        self.assertEqual(calls[1], ((base + timedelta(days=2)).isoformat(), (base + timedelta(days=4)).isoformat()))
        self.assertEqual(plan["contract_count"], 2)
        self.assertEqual(plan["hours"], 48)
        self.assertEqual(plan["skipped_hours"], 24)
        self.assertFalse(any((base + timedelta(days=1)) <= datetime.fromisoformat(row["start"]) < (base + timedelta(days=2))
                             for row in plan["rows"]))
        self.assertAlmostEqual(plan["calculated_cost"], 24 * 0.3 + 24 * 0.5)

    def test_apply_imports_then_adjusts_current_sum(self):
        plan = self._plan()
        last = datetime.fromisoformat(plan["rows"][-1]["start"])
        future = datetime.fromisoformat(plan["future_time"])
        confirmed = [
            {"start": int(last.timestamp() * 1000), "sum": plan["rows"][-1]["sum"]},
            {"start": int(future.timestamp() * 1000), "sum": plan["future_sum_before"] + plan["adjustment"]},
        ]
        with patch.object(history, "websocket_command") as ws, patch.object(history, "_rows", return_value=confirmed):
            history.apply_plan(plan)
        self.assertEqual([call.args[0]["type"] for call in ws.call_args_list],
                         ["recorder/import_statistics", "recorder/adjust_sum_statistics"])
        self.assertEqual(ws.call_args.args[0]["start_time"], plan["end"])


if __name__ == "__main__":
    unittest.main()
