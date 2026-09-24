from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from oil_supply.acceptance import run as acceptance_run
from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from oil_supply.quality import QualityService


OLD_USERS_DDL = """
CREATE TABLE supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
)
"""


class QualityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = QualityService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("audit", "auditor"),
            ("lab-1", "inspector"),
            ("lab-2", "inspector"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_facility("plan", {"facility_id": "city-depot", "name": "城市配送库", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "200000"})
        self.service.create_route("plan", {"route_id": "rack-b-city", "origin_id": "terminal-b", "destination_id": "city-depot", "product": "gasoline-95", "daily_capacity": "20000", "loss_basis_points": 10, "transit_hours": 12})
        for lot_id, quantity, cost in (("lot-g95-a", "9000", "780"), ("lot-g95-b", "9000", "782"), ("lot-g95-c", "6000", "779"), ("lot-g92-d", "4000", "770")):
            product = "gasoline-92" if lot_id == "lot-g92-d" else "gasoline-95"
            grade = "92" if lot_id == "lot-g92-d" else "95"
            self.service.add_inventory_lot("dispatch", {"lot_id": lot_id, "facility_id": "terminal-b", "product": product, "grade": grade, "quantity_barrels": quantity, "unit_cost_usd": cost, "received_at": "2026-09-24T05:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def _failing_version(self) -> int:
        self.service.create_sample("lab-1", {"sample_id": "samp-a-1", "lot_id": "lot-g95-a", "sample_kind": "retest", "drawn_at": "2026-09-24T07:30:00Z"})
        version = self.service.record_test("lab-1", {"sample_id": "samp-a-1", "metric": "octane_ron", "result_value": "94.1", "conclusion": "fail", "tested_at": "2026-09-24T07:45:00Z"})
        self.service.confirm_test("lab-2", version["version_id"])
        return version["version_id"]

    def _blend_genealogy(self) -> None:
        self.service.record_blend("lab-1", {"blend_id": "blend-1", "target": {"lot_id": "lot-mix-1", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-g95-a", "quantity_barrels": "3000"}, {"source_lot_id": "lot-g95-b", "quantity_barrels": "9000"}]})
        self.service.record_blend("lab-1", {"blend_id": "blend-2", "target": {"lot_id": "lot-mix-2", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-mix-1", "quantity_barrels": "6000"}, {"source_lot_id": "lot-g95-c", "quantity_barrels": "6000"}]})

    def _open_case(self) -> dict[str, object]:
        version_id = self._failing_version()
        self._blend_genealogy()
        return self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "辛烷值复测不合格"})

    # ---- 样品与检测版本 -------------------------------------------------

    def test_test_versions_are_append_only_and_confirmed_by_another_person(self) -> None:
        self.service.create_sample("lab-1", {"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        first = self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "octane_ron", "result_value": "95.4", "conclusion": "pass", "tested_at": "2026-09-24T07:00:00Z"})
        with self.assertRaises(Forbidden):
            self.service.confirm_test("lab-1", first["version_id"])
        confirmed = self.service.confirm_test("lab-2", first["version_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        second = self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "octane_ron", "result_value": "94.1", "conclusion": "fail", "tested_at": "2026-09-24T07:45:00Z"})
        self.assertEqual(second["version_no"], 2)
        rows = self.connection.execute("SELECT * FROM quality_test_versions ORDER BY version_no").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_version_id"], rows[0]["version_id"])
        self.assertEqual(rows[0]["state"], "confirmed")
        with self.assertRaises(InvalidState):
            self.service.confirm_test("lab-2", first["version_id"])
        self.service.confirm_test("lab-2", second["version_id"])
        with self.assertRaises(InvalidState):
            self.service.confirm_test("lab-2", second["version_id"])

    def test_only_latest_version_can_be_confirmed(self) -> None:
        self.service.create_sample("lab-1", {"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        first = self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "sulfur", "result_value": "8", "conclusion": "pass", "tested_at": "2026-09-24T07:00:00Z"})
        self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "sulfur", "result_value": "9", "conclusion": "pass", "tested_at": "2026-09-24T07:30:00Z"})
        with self.assertRaises(InvalidState):
            self.service.confirm_test("lab-2", first["version_id"])

    def test_sample_and_test_validation(self) -> None:
        with self.assertRaises(NotFound):
            self.service.create_sample("lab-1", {"sample_id": "samp-x", "lot_id": "missing", "drawn_at": "2026-09-24T06:30:00Z"})
        with self.assertRaises(Forbidden):
            self.service.create_sample("dispatch", {"sample_id": "samp-x", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        self.service.create_sample("lab-1", {"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        with self.assertRaises(Conflict):
            self.service.create_sample("lab-1", {"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        with self.assertRaises(ValidationFailed):
            self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "unknown", "result_value": "1", "conclusion": "pass", "tested_at": "2026-09-24T07:00:00Z"})

    # ---- 混兑谱系 --------------------------------------------------------

    def test_blend_conserves_mass_and_weighted_cost(self) -> None:
        self.service.record_blend("lab-1", {"blend_id": "blend-1", "target": {"lot_id": "lot-mix-1", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-g95-a", "quantity_barrels": "3000"}, {"source_lot_id": "lot-g95-b", "quantity_barrels": "9000"}]})
        mix1 = self.service.inventory_lot("lot-mix-1")
        self.assertEqual(mix1["quantity_barrels"], "12000.000")
        self.assertEqual(mix1["available_barrels"], "12000.000")
        self.assertEqual(mix1["unit_cost_usd"], "781.5000")
        self.assertEqual(self.service.inventory_lot("lot-g95-a")["available_barrels"], "6000.000")
        self.assertEqual(self.service.inventory_lot("lot-g95-b")["available_barrels"], "0.000")
        self.service.record_blend("lab-1", {"blend_id": "blend-2", "target": {"lot_id": "lot-mix-2", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-mix-1", "quantity_barrels": "6000"}, {"source_lot_id": "lot-g95-c", "quantity_barrels": "6000"}]})
        mix2 = self.service.inventory_lot("lot-mix-2")
        self.assertEqual(mix2["quantity_barrels"], "12000.000")
        self.assertEqual(mix2["available_barrels"], "12000.000")
        self.assertEqual(mix2["unit_cost_usd"], "780.2500")
        self.assertEqual(self.service.inventory_lot("lot-mix-1")["available_barrels"], "6000.000")
        self.assertEqual(self.service.inventory_lot("lot-g95-c")["available_barrels"], "0.000")

    def test_blend_rejects_unknown_or_insufficient_source(self) -> None:
        with self.assertRaises(NotFound):
            self.service.record_blend("lab-1", {"blend_id": "blend-x", "target": {"lot_id": "lot-x", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "missing", "quantity_barrels": "10"}]})
        with self.assertRaises(Conflict):
            self.service.record_blend("lab-1", {"blend_id": "blend-y", "target": {"lot_id": "lot-y", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-g95-a", "quantity_barrels": "99999"}]})
        with self.assertRaises(ValidationFailed):
            self.service.record_blend("lab-1", {"blend_id": "blend-z", "target": {"lot_id": "lot-z", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": []})

    # ---- 隔离传播 --------------------------------------------------------

    def test_quarantine_propagates_along_quantity_genealogy(self) -> None:
        case = self._open_case()
        holds = {item["lot_id"]: item["held_barrels"] for item in case["items"]}
        self.assertEqual(holds, {"lot-g95-a": "6000.000", "lot-mix-1": "1500.000", "lot-mix-2": "1500.000"})
        ratios = {item["lot_id"]: item["impact_ratio"] for item in case["items"]}
        self.assertEqual(ratios["lot-g95-a"], "1.000000")
        self.assertEqual(ratios["lot-mix-1"], "0.250000")
        self.assertEqual(ratios["lot-mix-2"], "0.125000")
        # 同罐无关批次不被锁死
        unrelated = self.service.lot_trace("audit", "lot-g92-d")
        self.assertEqual(unrelated["availability"]["held_barrels"], "0.000")
        self.assertEqual(unrelated["availability"]["usable_barrels"], "4000.000")
        self.assertEqual(unrelated["impacts"], [])
        clean = self.service.lot_trace("audit", "lot-g95-b")
        self.assertEqual(clean["impacts"], [])

    def test_quarantine_requires_confirmed_fail_version(self) -> None:
        self.service.create_sample("lab-1", {"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"})
        draft = self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "octane_ron", "result_value": "94.1", "conclusion": "fail", "tested_at": "2026-09-24T07:00:00Z"})
        with self.assertRaises(InvalidState):
            self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": draft["version_id"], "reason": "复测不合格"})
        passing = self.service.record_test("lab-1", {"sample_id": "samp-1", "metric": "sulfur", "result_value": "8", "conclusion": "pass", "tested_at": "2026-09-24T07:05:00Z"})
        self.service.confirm_test("lab-2", passing["version_id"])
        with self.assertRaises(ValidationFailed):
            self.service.open_quarantine("risk", {"case_id": "case-2", "root_lot_id": "lot-g95-a", "test_version_id": passing["version_id"], "reason": "误判"})
        with self.assertRaises(ValidationFailed):
            self.service.open_quarantine("risk", {"case_id": "case-3", "root_lot_id": "lot-g95-b", "test_version_id": passing["version_id"], "reason": "样品不属于该批次"})

    def test_duplicate_open_case_is_rejected(self) -> None:
        version_id = self._failing_version()
        self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "复测不合格"})
        with self.assertRaises(Conflict):
            self.service.open_quarantine("risk", {"case_id": "case-2", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "重复立案"})

    def test_quarantine_uses_latest_confirmed_conclusion(self) -> None:
        version_id = self._failing_version()
        # 未确认的草稿不阻碍依据已确认结论立案
        self.service.record_test("lab-1", {"sample_id": "samp-a-1", "metric": "octane_ron", "result_value": "95.1", "conclusion": "pass", "tested_at": "2026-09-24T08:00:00Z"})
        case = self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "复测不合格"})
        self.assertEqual(case["status"], "open")
        self.service.release_quarantine("risk", "case-1", {"items": [{"lot_id": "lot-g95-a", "dispositions": [{"action": "release", "quantity_barrels": "9000"}]}]})
        # 更新的确认版本取代旧结论后，旧版本不能再立案
        latest = self.connection.execute("SELECT MAX(version_id) AS vid FROM quality_test_versions").fetchone()["vid"]
        self.service.confirm_test("lab-2", latest)
        with self.assertRaises(InvalidState):
            self.service.open_quarantine("risk", {"case_id": "case-2", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "旧结论"})

    # ---- 发运与预留拦截 ---------------------------------------------------

    def _nominate_and_allocate(self, nomination_id: str, requested: str) -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": nomination_id, "route_id": "rack-b-city", "shipper_id": "city-retail", "service_date": "2026-09-26", "requested_barrels": requested, "priority": 10, "idempotency_key": f"key-{nomination_id}"})
        self.service.allocate("dispatch", "rack-b-city", "2026-09-26")

    def test_dispatch_and_reservation_reject_held_quantity(self) -> None:
        self._open_case()
        # lot-mix-1 账面 6000，隔离 1500，可动用 4500
        self._nominate_and_allocate("nom-1", "5000")
        with self.assertRaises(Conflict):
            self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-mix-1", 2)
        with self.assertRaises(Conflict):
            self.service.reserve_inventory("dispatch", {"reservation_id": "resv-x", "lot_id": "lot-mix-1", "quantity_barrels": "5000", "idempotency_key": "resv-key-x"})
        reservation = self.service.reserve_inventory("dispatch", {"reservation_id": "resv-1", "lot_id": "lot-mix-1", "quantity_barrels": "4000", "idempotency_key": "resv-key-1"})
        self.assertEqual(reservation["state"], "active")
        # 预留进一步压缩可动用数量
        with self.assertRaises(Conflict):
            self.service.reserve_inventory("dispatch", {"reservation_id": "resv-2", "lot_id": "lot-mix-1", "quantity_barrels": "600", "idempotency_key": "resv-key-2"})
        replay = self.service.reserve_inventory("dispatch", {"reservation_id": "resv-1", "lot_id": "lot-mix-1", "quantity_barrels": "4000", "idempotency_key": "resv-key-1"})
        self.assertEqual(replay, reservation)
        cancelled = self.service.cancel_reservation("dispatch", "resv-1")
        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaises(InvalidState):
            self.service.cancel_reservation("dispatch", "resv-1")

    def test_in_transit_transfer_is_flagged_not_rolled_back(self) -> None:
        self._blend_genealogy()
        self._nominate_and_allocate("nom-1", "2000")
        self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-mix-1", 2)
        version_id = self._failing_version()
        case = self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "复测不合格"})
        self.assertEqual(case["flagged_transfers"], ["transfer-1"])
        transfer = self.connection.execute("SELECT * FROM transfers WHERE transfer_id='transfer-1'").fetchone()
        self.assertEqual(transfer["state"], "in_transit")
        disposed = self.service.dispose_transfer("risk", "transfer-1", {"case_id": "case-1", "note": "到站复检合格，放行"})
        self.assertEqual(disposed["status"], "disposed")
        with self.assertRaises(InvalidState):
            self.service.dispose_transfer("risk", "transfer-1", {"case_id": "case-1", "note": "重复处置"})

    # ---- 解除隔离 --------------------------------------------------------

    def test_release_requires_mass_conservation(self) -> None:
        self._open_case()
        with self.assertRaises(ValidationFailed):
            self.service.release_quarantine("risk", "case-1", {"items": [{"lot_id": "lot-g95-a", "dispositions": [{"action": "release", "quantity_barrels": "5000"}]}]})
        with self.assertRaises(NotFound):
            self.service.release_quarantine("risk", "case-1", {"items": [{"lot_id": "lot-g92-d", "dispositions": []}]})

    def test_release_closes_case_and_conserves_quantities(self) -> None:
        case = self._open_case()
        self.assertEqual(case["flagged_transfers"], [])
        released = self.service.release_quarantine("risk", "case-1", {"items": [
            {"lot_id": "lot-g95-a", "dispositions": [{"action": "downgrade", "quantity_barrels": "6000"}], "note": "降级调和"},
            {"lot_id": "lot-mix-1", "dispositions": [{"action": "release", "quantity_barrels": "1500"}]},
            {"lot_id": "lot-mix-2", "dispositions": [{"action": "release", "quantity_barrels": "1000"}, {"action": "destroy", "quantity_barrels": "500"}]},
        ]})
        self.assertEqual(released["status"], "closed")
        self.assertEqual(self.service.inventory_lot("lot-g95-a")["available_barrels"], "0.000")
        self.assertEqual(self.service.inventory_lot("lot-mix-1")["available_barrels"], "6000.000")
        self.assertEqual(self.service.inventory_lot("lot-mix-2")["available_barrels"], "11500.000")
        adjustments = self.connection.execute("SELECT * FROM inventory_adjustments ORDER BY adjustment_id").fetchall()
        self.assertEqual([row["reason_code"] for row in adjustments], ["quarantine_downgrade", "quarantine_destroy"])
        with self.assertRaises(InvalidState):
            self.service.release_quarantine("risk", "case-1", {"items": [{"lot_id": "lot-mix-1", "dispositions": [{"action": "release", "quantity_barrels": "1500"}]}]})

    def test_case_stays_open_until_transfers_disposed(self) -> None:
        self._blend_genealogy()
        self._nominate_and_allocate("nom-1", "2000")
        self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-mix-1", 2)
        version_id = self._failing_version()
        self.service.open_quarantine("risk", {"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "复测不合格"})
        partial = self.service.release_quarantine("risk", "case-1", {"items": [
            {"lot_id": "lot-g95-a", "dispositions": [{"action": "release", "quantity_barrels": "6000"}]},
            {"lot_id": "lot-mix-1", "dispositions": [{"action": "release", "quantity_barrels": "1000"}]},
            {"lot_id": "lot-mix-2", "dispositions": [{"action": "release", "quantity_barrels": "1500"}]},
        ]})
        self.assertEqual(partial["status"], "open")
        self.service.dispose_transfer("risk", "transfer-1", {"case_id": "case-1", "note": "到站复检合格，放行"})
        closed = self.service.quarantine_case("audit", "case-1")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_by"], "risk")

    # ---- 运营视图 --------------------------------------------------------

    def test_lot_trace_exposes_sources_impact_availability_and_chain(self) -> None:
        self._open_case()
        self.service.reserve_inventory("dispatch", {"reservation_id": "resv-1", "lot_id": "lot-mix-2", "quantity_barrels": "1000", "idempotency_key": "resv-key-1"})
        trace = self.service.lot_trace("audit", "lot-mix-2")
        self.assertEqual({row["source_lot_id"] for row in trace["sources"]}, {"lot-mix-1", "lot-g95-c", "lot-g95-a", "lot-g95-b"})
        depth_two = {row["source_lot_id"] for row in trace["sources"] if row["depth"] == 2}
        self.assertEqual(depth_two, {"lot-g95-a", "lot-g95-b"})
        self.assertEqual(trace["impacts"][0]["impact_ratio"], "0.125000")
        self.assertEqual(trace["impacts"][0]["held_barrels"], "1500.000")
        availability = trace["availability"]
        self.assertEqual(availability["available_barrels"], "12000.000")
        self.assertEqual(availability["held_barrels"], "1500.000")
        self.assertEqual(availability["reserved_barrels"], "1000.000")
        self.assertEqual(availability["usable_barrels"], "9500.000")
        event_types = [event["event_type"] for event in trace["decision_chain"]]
        self.assertIn("blend.recorded", event_types)
        self.assertIn("quarantine.opened", event_types)
        self.assertIn("inventory.reserved", event_types)
        root_trace = self.service.lot_trace("audit", "lot-g95-a")
        root_events = [event["event_type"] for event in root_trace["decision_chain"]]
        self.assertIn("quality.sampled", root_events)
        self.assertIn("quality.test_recorded", root_events)
        self.assertIn("quality.test_confirmed", root_events)

    def test_trace_requires_permission_and_existing_lot(self) -> None:
        with self.assertRaises(NotFound):
            self.service.lot_trace("audit", "missing")
        self.service.create_user("inactive", "停用用户", "inspector")
        self.connection.execute("UPDATE supply_users SET active=0 WHERE user_id='inactive'")
        with self.assertRaises(Forbidden):
            self.service.lot_trace("inactive", "lot-g95-a")

    # ---- HTTP 边界 --------------------------------------------------------

    def test_api_routes_cover_quality_flow(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "lab-1"}
        sample = app.handle("POST", "/quality/samples", headers, json.dumps({"sample_id": "samp-1", "lot_id": "lot-g95-a", "drawn_at": "2026-09-24T06:30:00Z"}).encode())
        self.assertEqual(sample.status, 201)
        version = app.handle("POST", "/quality/tests", headers, json.dumps({"sample_id": "samp-1", "metric": "octane_ron", "result_value": "94.1", "conclusion": "fail", "tested_at": "2026-09-24T07:00:00Z"}).encode())
        self.assertEqual(version.status, 201)
        version_id = version.body["version_id"]
        same_person = app.handle("POST", f"/quality/tests/{version_id}/confirm", headers)
        self.assertEqual(same_person.status, 403)
        confirmed = app.handle("POST", f"/quality/tests/{version_id}/confirm", {"X-Actor-Id": "lab-2"})
        self.assertEqual(confirmed.status, 200)
        case = app.handle("POST", "/quarantine/cases", {"X-Actor-Id": "risk"}, json.dumps({"case_id": "case-1", "root_lot_id": "lot-g95-a", "test_version_id": version_id, "reason": "复测不合格"}).encode())
        self.assertEqual(case.status, 201)
        view = app.handle("GET", "/quarantine/cases/case-1", {"X-Actor-Id": "audit"})
        self.assertEqual(view.status, 200)
        self.assertEqual(view.body["items"][0]["lot_id"], "lot-g95-a")
        trace = app.handle("GET", "/inventory/lots/lot-g95-a/trace", {"X-Actor-Id": "audit"})
        self.assertEqual(trace.status, 200)
        self.assertEqual(trace.body["availability"]["held_barrels"], "9000.000")
        released = app.handle("POST", "/quarantine/cases/case-1/release", {"X-Actor-Id": "risk"}, json.dumps({"items": [{"lot_id": "lot-g95-a", "dispositions": [{"action": "release", "quantity_barrels": "9000"}]}]}).encode())
        self.assertEqual(released.status, 200)
        self.assertEqual(released.body["status"], "closed")
        reservation = app.handle("POST", "/inventory/reservations", {"X-Actor-Id": "dispatch"}, json.dumps({"reservation_id": "resv-1", "lot_id": "lot-g95-c", "quantity_barrels": "100", "idempotency_key": "resv-key-1"}).encode())
        self.assertEqual(reservation.status, 201)
        cancelled = app.handle("POST", "/inventory/reservations/resv-1/cancel", {"X-Actor-Id": "dispatch"})
        self.assertEqual(cancelled.status, 200)

    # ---- 角色迁移 --------------------------------------------------------

    def test_inspector_role_migration_preserves_existing_users(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(OLD_USERS_DDL)
            connection.execute("INSERT INTO supply_users(user_id,display_name,role,active,created_at) VALUES('legacy','旧用户','planner',1,'2026-01-01T00:00:00Z')")
            service = QualityService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
            service.create_user("lab", "质检员", "inspector")
            roles = {row["user_id"]: row["role"] for row in connection.execute("SELECT * FROM supply_users")}
            self.assertEqual(roles, {"legacy": "planner", "lab": "inspector"})
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(foreign_keys, [])
        finally:
            connection.close()

    # ---- 端到端验收 --------------------------------------------------------

    def test_offline_acceptance_covers_quarantine_flow(self) -> None:
        from pathlib import Path

        result = acceptance_run(Path(__file__).resolve().parents[1])
        self.assertEqual(result["status"], "ok")
        quality = result["quality"]
        self.assertEqual(quality["holds"], {"lot-g95-a": "6000.000", "lot-mix-1": "1000.000", "lot-mix-2": "1500.000"})
        self.assertEqual(quality["flagged_transfers"], ["transfer-t1"])
        self.assertTrue(quality["dispatch_rejected"])
        self.assertTrue(quality["reservation_rejected"])
        self.assertEqual(quality["case_status"], "closed")
        self.assertEqual(quality["trace"]["sources"], 4)
        self.assertEqual(quality["trace"]["usable_barrels"], "6500.000")
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
