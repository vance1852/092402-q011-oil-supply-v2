"""贯通报价、线路、库存、提名、情景分析和质量隔离的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict
from .quality import QualityService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = QualityService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("plan", "planner"),
        ("dispatch", "dispatcher"),
        ("risk", "risk"),
        ("audit", "auditor"),
        ("lab-1", "inspector"),
        ("lab-2", "inspector"),
    ):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_facility("plan", {"facility_id": "city-depot", "name": "城市配送库", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "200000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.create_route("plan", {"route_id": "rack-b-city", "origin_id": "terminal-b", "destination_id": "city-depot", "product": "gasoline-95", "daily_capacity": "20000", "loss_basis_points": 10, "transit_hours": 12})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")

    # 质量子域：95 号汽油辛烷值复测不一致，立案隔离并沿混兑谱系传播。
    for lot_id, quantity, cost in (("lot-g95-a", "9000", "780"), ("lot-g95-b", "9000", "782"), ("lot-g95-c", "6000", "779")):
        service.add_inventory_lot("dispatch", {"lot_id": lot_id, "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95", "quantity_barrels": quantity, "unit_cost_usd": cost, "received_at": "2026-09-24T05:00:00Z"})
    service.create_sample("lab-1", {"sample_id": "samp-a-1", "lot_id": "lot-g95-a", "sample_kind": "routine", "drawn_at": "2026-09-24T06:30:00Z"})
    initial = service.record_test("lab-1", {"sample_id": "samp-a-1", "metric": "octane_ron", "result_value": "95.4", "conclusion": "pass", "tested_at": "2026-09-24T07:00:00Z"})
    service.confirm_test("lab-2", initial["version_id"])
    service.create_sample("lab-1", {"sample_id": "samp-a-2", "lot_id": "lot-g95-a", "sample_kind": "retest", "drawn_at": "2026-09-24T07:30:00Z", "note": "辛烷值复测"})
    retest = service.record_test("lab-1", {"sample_id": "samp-a-2", "metric": "octane_ron", "result_value": "94.1", "conclusion": "fail", "tested_at": "2026-09-24T07:45:00Z", "note": "复测与初测不一致"})
    service.confirm_test("lab-2", retest["version_id"])
    service.record_blend("lab-1", {"blend_id": "blend-1", "target": {"lot_id": "lot-mix-1", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-g95-a", "quantity_barrels": "3000"}, {"source_lot_id": "lot-g95-b", "quantity_barrels": "9000"}], "note": "95号汽油调和"})
    service.record_blend("lab-1", {"blend_id": "blend-2", "target": {"lot_id": "lot-mix-2", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95"}, "lines": [{"source_lot_id": "lot-mix-1", "quantity_barrels": "6000"}, {"source_lot_id": "lot-g95-c", "quantity_barrels": "6000"}]})
    service.submit_nomination("dispatch", {"nomination_id": "nom-t1", "route_id": "rack-b-city", "shipper_id": "city-retail", "service_date": "2026-09-26", "requested_barrels": "2000", "priority": 10, "idempotency_key": "nom-key-t1"})
    service.allocate("dispatch", "rack-b-city", "2026-09-26")
    service.dispatch_transfer("dispatch", "transfer-t1", "nom-t1", "lot-mix-1", 2)
    case = service.open_quarantine("risk", {"case_id": "case-oct-1", "root_lot_id": "lot-g95-a", "test_version_id": retest["version_id"], "reason": "95号汽油辛烷值复测不合格"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-t2", "route_id": "rack-b-city", "shipper_id": "city-retail", "service_date": "2026-09-26", "requested_barrels": "3500", "priority": 20, "idempotency_key": "nom-key-t2"})
    service.allocate("dispatch", "rack-b-city", "2026-09-26")
    dispatch_rejected = False
    try:
        service.dispatch_transfer("dispatch", "transfer-t2", "nom-t2", "lot-mix-1", 2)
    except Conflict:
        dispatch_rejected = True
    reservation_rejected = False
    try:
        service.reserve_inventory("dispatch", {"reservation_id": "resv-x", "lot_id": "lot-mix-2", "quantity_barrels": "11000", "idempotency_key": "resv-key-x"})
    except Conflict:
        reservation_rejected = True
    reservation = service.reserve_inventory("dispatch", {"reservation_id": "resv-1", "lot_id": "lot-mix-2", "quantity_barrels": "5000", "idempotency_key": "resv-key-1"})
    service.dispose_transfer("risk", "transfer-t1", {"case_id": "case-oct-1", "note": "到站复检合格，放行交付"})
    released = service.release_quarantine("risk", "case-oct-1", {"items": [
        {"lot_id": "lot-g95-a", "dispositions": [{"action": "downgrade", "quantity_barrels": "6000"}], "note": "降级为92号汽油调和"},
        {"lot_id": "lot-mix-1", "dispositions": [{"action": "release", "quantity_barrels": "1000"}]},
        {"lot_id": "lot-mix-2", "dispositions": [{"action": "release", "quantity_barrels": "1000"}, {"action": "destroy", "quantity_barrels": "500"}], "note": "合格部分放行，不合格部分报废"},
    ]})
    trace = service.lot_trace("audit", "lot-mix-2")
    quality = {
        "case_id": case["case_id"],
        "holds": {item["lot_id"]: item["held_barrels"] for item in case["items"]},
        "flagged_transfers": case["flagged_transfers"],
        "dispatch_rejected": dispatch_rejected,
        "reservation_rejected": reservation_rejected,
        "reservation_id": reservation["reservation_id"],
        "case_status": released["status"],
        "trace": {
            "lot_id": "lot-mix-2",
            "sources": len(trace["sources"]),
            "usable_barrels": trace["availability"]["usable_barrels"],
            "decision_events": len(trace["decision_chain"]),
        },
    }
    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "quality": quality, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
