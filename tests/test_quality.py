from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound
from oil_supply.genealogy import propagate_impact
from oil_supply.service import SupplyService


class QualityIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for uid, role in (
            ("plan", "planner"),
            ("disp", "dispatcher"),
            ("risk", "risk"),
            ("qa1", "quality"),
            ("qa2", "quality"),
            ("aud", "auditor"),
        ):
            self.service.create_user(uid, uid, role)
        self.service.create_facility("plan", {"facility_id": "tank", "name": "储罐", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "100000"})
        self.service.create_facility("plan", {"facility_id": "term", "name": "终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "100000"})
        self.service.create_route("plan", {"route_id": "r1", "origin_id": "tank", "destination_id": "term", "product": "gasoline-95", "daily_capacity": "50000", "loss_basis_points": 0, "transit_hours": 10})

    def tearDown(self) -> None:
        self.connection.close()

    def lot(self, lot_id: str, quantity: str, grade: str = "95", product: str = "gasoline-95") -> None:
        self.service.add_inventory_lot("disp", {"lot_id": lot_id, "facility_id": "tank", "product": product, "grade": grade, "quantity_barrels": quantity, "unit_cost_usd": "90", "received_at": "2026-09-24T06:00:00Z"})

    def sample(self, sample_id: str = "s1", lot_id: str = "L1", quantity: str = "1000") -> dict:
        return self.service.register_quality_sample("qa1", {"sample_id": sample_id, "lot_id": lot_id, "sampled_quantity_barrels": quantity, "sampled_at": "2026-09-24T07:00:00Z"})

    def failing_confirmed_test(self, sample_id: str = "s1") -> int:
        test = self.service.record_test_version("qa1", {"sample_id": sample_id, "test_code": "RON", "measured_value": "93.0", "spec_min": "95", "spec_max": "99", "method": "GB/T 5487", "instrument_id": "eng-1"})
        test_id = test["test_version_id"]
        self.service.confirm_test_version("qa2", test_id, "初核")
        self.service.confirm_test_version("risk", test_id, "复核")
        return test_id

    def test_propagation_splits_by_actual_quantities_and_conserves(self) -> None:
        self.lot("L1", "1000")
        self.lot("L2", "1000")
        self.service.record_blend("disp", {"blend_id": "B1", "output_lot_id": "D1", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "400"}, {"lot_id": "L2", "quantity_barrels": "600"}]})
        tanks, transfers = propagate_impact(self.connection, "L1", Decimal("1000"))
        self.assertEqual(tanks["L1"], Decimal("600.000"))
        self.assertEqual(tanks["D1"], Decimal("400.000"))
        self.assertEqual(sum(tanks.values()) + sum(transfers.values()), Decimal("1000.000"))

    def test_test_versions_append_and_require_two_distinct_non_recorder_confirmers(self) -> None:
        self.lot("L1", "1000")
        self.sample()
        first = self.service.record_test_version("qa1", {"sample_id": "s1", "test_code": "RON", "measured_value": "93.0", "spec_min": "95", "spec_max": "99", "method": "meth-1", "instrument_id": "eng-1"})
        with self.assertRaises(Conflict):
            self.service.confirm_test_version("qa1", first["test_version_id"])
        self.service.confirm_test_version("qa2", first["test_version_id"])
        with self.assertRaises(InvalidState):
            self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})
        with self.assertRaises(Conflict):
            self.service.confirm_test_version("qa2", first["test_version_id"])
        self.service.confirm_test_version("risk", first["test_version_id"])
        second = self.service.record_test_version("qa1", {"sample_id": "s1", "test_code": "RON", "measured_value": "95.6", "spec_min": "95", "spec_max": "99", "method": "meth-1", "instrument_id": "eng-2"})
        self.assertEqual(second["version_no"], 2)
        self.assertEqual(first["conclusion"], "fail")
        self.assertEqual(second["conclusion"], "pass")
        rows = self.connection.execute("SELECT version_no FROM quality_test_versions ORDER BY version_no").fetchall()
        self.assertEqual([row["version_no"] for row in rows], [1, 2])

    def test_case_propagates_to_blend_and_in_transit_without_locking_unrelated_lot(self) -> None:
        self.lot("L1", "1000")
        self.lot("L2", "600")
        self.lot("OTHER", "500")
        self.service.record_blend("disp", {"blend_id": "B1", "output_lot_id": "D1", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "400"}, {"lot_id": "L2", "quantity_barrels": "600"}]})
        self.service.submit_nomination("disp", {"nomination_id": "n1", "route_id": "r1", "shipper_id": "sh1", "service_date": "2026-09-24", "requested_barrels": "300", "priority": 10, "idempotency_key": "k1"})
        self.service.allocate("disp", "r1", "2026-09-24")
        self.service.dispatch_transfer("disp", "t1", "n1", "L1", 2)
        self.sample()
        self.failing_confirmed_test()
        case = self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "RON 不合格"})
        scopes = {(t["scope"], t.get("lot_id") or t.get("transfer_id")): t["isolated_barrels"] for t in case["targets"]}
        self.assertEqual(scopes[("lot", "L1")], "300.000")
        self.assertEqual(scopes[("lot", "D1")], "400.000")
        self.assertEqual(scopes[("transfer", "t1")], "300.000")
        self.assertNotIn(("lot", "OTHER"), scopes)
        self.assertNotIn(("lot", "L2"), scopes)
        self.assertEqual(self.connection.execute("SELECT state FROM transfers WHERE transfer_id='t1'").fetchone()["state"], "held")
        with self.assertRaises(InvalidState):
            self.service.receive_transfer("disp", "t1")

    def test_reservation_and_dispatch_reject_affected_quantities_in_transaction(self) -> None:
        self.lot("L1", "1000")
        self.sample()
        self.failing_confirmed_test()
        self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "affected_quantity_barrels": "300", "reason": "x"})
        with self.assertRaises(Conflict):
            self.service.create_reservation("disp", {"reservation_id": "r1", "lot_id": "L1", "quantity_barrels": "701", "idempotency_key": "k1"})
        reserved = self.service.create_reservation("disp", {"reservation_id": "r2", "lot_id": "L1", "quantity_barrels": "700", "idempotency_key": "k2"})
        self.assertEqual(reserved["state"], "held")
        self.service.submit_nomination("disp", {"nomination_id": "n1", "route_id": "r1", "shipper_id": "sh1", "service_date": "2026-09-25", "requested_barrels": "1", "priority": 10, "idempotency_key": "k3"})
        self.service.allocate("disp", "r1", "2026-09-25")
        with self.assertRaises(Conflict):
            self.service.dispatch_transfer("disp", "t9", "n1", "L1", 2)
        self.assertEqual(self.service.inventory_lot("L1")["available_barrels"], "1000")
        self.service.release_reservation("disp", "r2")
        self.assertEqual(
            self.service.create_reservation("disp", {"reservation_id": "r3", "lot_id": "L1", "quantity_barrels": "700", "idempotency_key": "k4"})["state"],
            "held",
        )

    def test_reservation_overlap_blocks_case_opening(self) -> None:
        self.lot("L1", "1000")
        self.sample()
        self.failing_confirmed_test()
        self.service.create_reservation("disp", {"reservation_id": "r1", "lot_id": "L1", "quantity_barrels": "800", "idempotency_key": "k1"})
        with self.assertRaises(Conflict):
            self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})

    def test_release_requires_conservative_disposition_of_every_target(self) -> None:
        self.lot("L1", "1000")
        self.sample()
        self.failing_confirmed_test()
        case = self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})
        target_id = case["targets"][0]["target_id"]
        with self.assertRaises(Exception) as missing:
            self.service.release_isolation_case("risk", "c1", {"note": "n", "dispositions": []})
        self.assertEqual(missing.exception.code, "validation_failed")
        with self.assertRaises(Exception) as unequal:
            self.service.release_isolation_case("risk", "c1", {"note": "n", "dispositions": [{"target_id": target_id, "action": "release", "quantity_barrels": "999"}]})
        self.assertEqual(unequal.exception.code, "validation_failed")
        with self.assertRaises(Exception) as no_note:
            self.service.release_isolation_case("risk", "c1", {"note": "  ", "dispositions": [{"target_id": target_id, "action": "release", "quantity_barrels": "1000"}]})
        self.assertEqual(no_note.exception.code, "validation_failed")
        released = self.service.release_isolation_case("risk", "c1", {"note": "复检合格放行", "dispositions": [{"target_id": target_id, "action": "release", "quantity_barrels": "1000"}]})
        self.assertEqual(released["state"], "released")
        with self.assertRaises(InvalidState):
            self.service.release_isolation_case("risk", "c1", {"note": "再次", "dispositions": [{"target_id": target_id, "action": "release", "quantity_barrels": "1000"}]})

    def test_disposition_actions_conserve_mass_and_transfer_history_is_not_rolled_back(self) -> None:
        self.lot("L1", "1000")
        self.lot("L2", "600")
        self.service.record_blend("disp", {"blend_id": "B1", "output_lot_id": "D1", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "400"}, {"lot_id": "L2", "quantity_barrels": "600"}]})
        self.service.submit_nomination("disp", {"nomination_id": "n1", "route_id": "r1", "shipper_id": "sh1", "service_date": "2026-09-24", "requested_barrels": "300", "priority": 10, "idempotency_key": "k1"})
        self.service.allocate("disp", "r1", "2026-09-24")
        departed = self.service.dispatch_transfer("disp", "t1", "n1", "L1", 2)
        self.assertEqual(departed["loaded_barrels"], "300.000")
        self.sample()
        self.failing_confirmed_test()
        case = self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})
        by_scope = {(t["scope"], t.get("lot_id") or t.get("transfer_id")): t["target_id"] for t in case["targets"]}
        with self.assertRaises(Exception) as bad_transit:
            self.service.release_isolation_case("risk", "c1", {"note": "n", "dispositions": [
                {"target_id": by_scope[("lot", "L1")], "action": "destroy", "quantity_barrels": "300"},
                {"target_id": by_scope[("lot", "D1")], "action": "release", "quantity_barrels": "400"},
                {"target_id": by_scope[("transfer", "t1")], "action": "downgrade", "quantity_barrels": "300", "destination_lot_id": "X", "product": "gasoline-92", "grade": "92"},
            ]})
        self.assertEqual(bad_transit.exception.code, "validation_failed")
        self.service.release_isolation_case("risk", "c1", {"note": "销毁/降级/在途放行", "dispositions": [
            {"target_id": by_scope[("lot", "L1")], "action": "destroy", "quantity_barrels": "300"},
            {"target_id": by_scope[("lot", "D1")], "action": "downgrade", "quantity_barrels": "250", "destination_lot_id": "D92", "product": "gasoline-92", "grade": "92"},
            {"target_id": by_scope[("lot", "D1")], "action": "release", "quantity_barrels": "150"},
            {"target_id": by_scope[("transfer", "t1")], "action": "release", "quantity_barrels": "300"},
        ]})
        # L1 初始 1000，混兑配料 400 后总量 600；发运只扣可用量，
        # 罐内剩余 300 被监督销毁。
        self.assertEqual(dict(self.connection.execute("SELECT quantity_barrels,available_barrels FROM inventory_lots WHERE lot_id='L1'").fetchone()), {"quantity_barrels": "300.000", "available_barrels": "0.000"})
        d1 = dict(self.connection.execute("SELECT quantity_barrels,available_barrels FROM inventory_lots WHERE lot_id='D1'").fetchone())
        self.assertEqual(d1, {"quantity_barrels": "750.000", "available_barrels": "750.000"})
        self.assertEqual(dict(self.connection.execute("SELECT quantity_barrels,available_barrels FROM inventory_lots WHERE lot_id='D92'").fetchone()), {"quantity_barrels": "250.000", "available_barrels": "250.000"})
        transfer = self.connection.execute("SELECT loaded_barrels,state FROM transfers WHERE transfer_id='t1'").fetchone()
        self.assertEqual(transfer["state"], "in_transit")
        self.assertEqual(transfer["loaded_barrels"], "300.000")
        received = self.service.receive_transfer("disp", "t1")
        self.assertEqual(received["state"], "delivered")

    def test_lot_trace_reports_sources_impact_ratio_free_quantity_and_chain(self) -> None:
        self.lot("L1", "1000")
        self.lot("L2", "600")
        self.sample()
        self.service.record_blend("disp", {"blend_id": "B1", "output_lot_id": "D1", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "400"}, {"lot_id": "L2", "quantity_barrels": "600"}]})
        self.failing_confirmed_test()
        self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})
        trace = self.service.lot_trace("D1")
        self.assertEqual(trace["free_barrels"], "600.000")
        self.assertEqual(trace["isolated_open_barrels"], "400.000")
        self.assertEqual(trace["available_barrels"], "1000")
        self.assertEqual(trace["quality_impacts"][0]["impact_percent"], "40.0000")
        sources = {item["lot_id"]: item["quantity_barrels"] for item in trace["sources"]}
        self.assertEqual(sources, {"L1": "400", "L2": "600"})
        types = [item["type"] for item in trace["decision_chain"]]
        self.assertEqual(types, ["sample.registered", "test.recorded", "test.confirmed", "test.confirmed", "case.opened"])
        self.assertEqual(trace["decision_chain"][-1]["targets"][0]["lot_id"], "L1")

    def test_blend_conserves_mass_and_blocks_isolated_or_reserved_ingredients(self) -> None:
        self.lot("L1", "1000")
        with self.assertRaises(Exception) as mismatch:
            self.service.record_blend("disp", {"blend_id": "B0", "output_lot_id": "D0", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "999", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "1000"}]})
        self.assertEqual(mismatch.exception.code, "validation_failed")
        self.sample()
        self.failing_confirmed_test()
        self.service.open_isolation_case("risk", {"case_id": "c1", "sample_id": "s1", "reason": "x"})
        with self.assertRaises(Conflict):
            self.service.record_blend("disp", {"blend_id": "B1", "output_lot_id": "D1", "facility_id": "tank", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T07:30:00Z", "ingredients": [{"lot_id": "L1", "quantity_barrels": "1000"}]})

    def test_permissions_enforced(self) -> None:
        self.lot("L1", "1000")
        with self.assertRaises(Forbidden):
            self.service.register_quality_sample("disp", {"sample_id": "s1", "lot_id": "L1", "sampled_quantity_barrels": "1000", "sampled_at": "2026-09-24T07:00:00Z"})
        self.sample()
        test = self.service.record_test_version("qa1", {"sample_id": "s1", "test_code": "RON", "measured_value": "93", "spec_min": "95", "spec_max": "99", "method": "meth-1", "instrument_id": "eng-1"})
        with self.assertRaises(Forbidden):
            self.service.confirm_test_version("aud", test["test_version_id"])
        self.service.confirm_test_version("qa2", test["test_version_id"])
        self.service.confirm_test_version("risk", test["test_version_id"])
        with self.assertRaises(Forbidden):
            self.service.open_isolation_case("qa1", {"case_id": "c1", "sample_id": "s1", "reason": "x"})

    def test_http_routes_and_missing_actor(self) -> None:
        self.lot("L1", "1000")
        app = JsonApplication(self.service)
        sample_body = b'{"sample_id":"s1","lot_id":"L1","sampled_quantity_barrels":"1000","sampled_at":"2026-09-24T07:00:00Z"}'
        response = app.handle("POST", "/quality/samples", {}, sample_body)
        self.assertEqual(response.status, 422)
        created = app.handle("POST", "/quality/samples", {"X-Actor-Id": "qa1"}, sample_body)
        self.assertEqual(created.status, 201)
        test = app.handle("POST", "/quality/tests", {"X-Actor-Id": "qa1"}, b'{"sample_id":"s1","test_code":"RON","measured_value":"93","spec_min":"95","spec_max":"99","method":"meth-1","instrument_id":"eng-1"}')
        test_id = test.body["test_version_id"]
        self.assertEqual(app.handle("POST", f"/quality/tests/{test_id}/confirm", {"X-Actor-Id": "qa2"}, b'{"note":"ok"}').status, 200)
        self.assertEqual(app.handle("POST", f"/quality/tests/{test_id}/confirm", {"X-Actor-Id": "risk"}, b'{}').status, 200)
        case = app.handle("POST", "/isolation/cases", {"X-Actor-Id": "risk"}, b'{"case_id":"c1","sample_id":"s1","reason":"x"}')
        self.assertEqual(case.status, 201)
        trace = app.handle("GET", "/inventory/lots/L1/trace", {"X-Actor-Id": "aud"})
        self.assertEqual(trace.status, 200)
        self.assertEqual(trace.body["free_barrels"], "0.000")
        self.assertEqual(app.handle("GET", "/isolation/cases/c1", {"X-Actor-Id": "risk"}).body["state"], "open")
        with self.assertRaises(NotFound):
            self.service.lot_trace("missing")


if __name__ == "__main__":
    unittest.main()
