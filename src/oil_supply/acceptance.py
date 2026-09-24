"""贯通报价、线路、库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"), ("qa1", "quality"), ("qa2", "quality")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    service.add_inventory_lot("dispatch", {"lot_id": "g95-a", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95", "quantity_barrels": "1000", "unit_cost_usd": "95", "received_at": "2026-09-24T06:30:00Z"})
    service.add_inventory_lot("dispatch", {"lot_id": "g95-b", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95", "quantity_barrels": "600", "unit_cost_usd": "96", "received_at": "2026-09-24T06:31:00Z"})
    service.register_quality_sample("qa1", {"sample_id": "smp-g95", "lot_id": "g95-a", "sampled_quantity_barrels": "1000", "sampled_at": "2026-09-24T06:40:00Z"})
    service.record_blend("dispatch", {"blend_id": "bln-g95", "output_lot_id": "g95-d", "facility_id": "terminal-b", "product": "gasoline-95", "grade": "95", "output_quantity_barrels": "1000", "occurred_at": "2026-09-24T06:50:00Z", "ingredients": [{"lot_id": "g95-a", "quantity_barrels": "400"}, {"lot_id": "g95-b", "quantity_barrels": "600"}]})
    quality_case = service.record_test_version("qa1", {"sample_id": "smp-g95", "test_code": "RON", "measured_value": "93.4", "spec_min": "95", "spec_max": "99", "method": "GB/T 5487", "instrument_id": "ron-bench-1"})
    service.confirm_test_version("qa2", quality_case["test_version_id"], "实验室初核")
    service.confirm_test_version("risk", quality_case["test_version_id"], "风险复核")
    isolation = service.open_isolation_case("risk", {"case_id": "iso-g95", "sample_id": "smp-g95", "reason": "95 号汽油 RON 复测不一致，下游混兑批次同步隔离"})
    release_targets = {target["lot_id"]: target["target_id"] for target in isolation["targets"] if target["scope"] == "lot"}
    released = service.release_isolation_case("risk", "iso-g95", {"note": "罐内余油监督销毁；混兑受影响数量降级为 92 号汽油", "dispositions": [
        {"target_id": release_targets["g95-a"], "action": "destroy", "quantity_barrels": "600"},
        {"target_id": release_targets["g95-d"], "action": "downgrade", "quantity_barrels": "400", "destination_lot_id": "g92-d", "product": "gasoline-92", "grade": "92", "note": "降级改牌号"},
    ]})
    trace = service.lot_trace("g92-d")
    quality = {"case_id": isolation["case_id"], "targets": len(isolation["targets"]), "released": released, "trace_free_barrels": trace["free_barrels"], "sources": [source["lot_id"] for source in trace["sources"]]}
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
