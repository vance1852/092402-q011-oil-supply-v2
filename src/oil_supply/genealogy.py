"""按实际数量谱系计算质量影响与批次可用量。

谱系边有两类：

- 混兑：原料批次的油按配料数量进入产出批次；
- 转运：已发运且仍在途的数量随车离开储罐，交付后成为不可回滚的历史。

隔离传播沿这些边按比例分配受影响数量；已交付部分不在可追溯池内，
不会被回滚，仍在途的转运由调用方标记为待处置。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from .errors import InvalidState
from .planning import decimal_text, quantize_volume


ZERO = Decimal("0")


def held_reservation_barrels(connection: sqlite3.Connection, lot_id: str) -> Decimal:
    row = connection.execute(
        "SELECT COALESCE(SUM(CAST(quantity_barrels AS REAL)),0) AS held "
        "FROM inventory_reservations WHERE lot_id=? AND state='held'",
        (lot_id,),
    ).fetchone()
    return quantize_volume(Decimal(str(row["held"])))


def open_isolation_barrels(connection: sqlite3.Connection, lot_id: str) -> Decimal:
    """当前仍被未结案件锁定的罐内数量。"""
    row = connection.execute(
        "SELECT COALESCE(SUM(CAST(t.isolated_barrels AS REAL)),0) AS isolated "
        "FROM isolation_targets t JOIN isolation_cases c ON c.case_id=t.case_id "
        "WHERE t.scope='lot' AND t.lot_id=? AND c.state='open' AND t.disposition_status='pending'",
        (lot_id,),
    ).fetchone()
    return quantize_volume(Decimal(str(row["isolated"])))


def lot_free_barrels(connection: sqlite3.Connection, lot_id: str) -> Decimal:
    """可用于新发运、预留或混兑配料的数量：账面可用扣除预留与隔离。"""
    row = connection.execute(
        "SELECT available_barrels FROM inventory_lots WHERE lot_id=?", (lot_id,)
    ).fetchone()
    if row is None:
        raise InvalidState("库存批次不存在")
    available = Decimal(row["available_barrels"])
    return quantize_volume(
        available
        - held_reservation_barrels(connection, lot_id)
        - open_isolation_barrels(connection, lot_id)
    )


def _lot_pool(connection: sqlite3.Connection, lot_id: str) -> dict[str, object]:
    lot = connection.execute(
        "SELECT quantity_barrels,available_barrels FROM inventory_lots WHERE lot_id=?",
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise InvalidState(f"库存批次 {lot_id} 不存在")
    transfers = connection.execute(
        "SELECT transfer_id,loaded_barrels FROM transfers "
        "WHERE inventory_lot_id=? AND state IN ('in_transit','held')",
        (lot_id,),
    ).fetchall()
    blends = connection.execute(
        "SELECT bi.blend_id,bi.quantity_barrels,b.output_lot_id "
        "FROM blend_ingredients bi JOIN blends b ON b.blend_id=bi.blend_id "
        "WHERE bi.input_lot_id=?",
        (lot_id,),
    ).fetchall()
    available = Decimal(lot["available_barrels"])
    # 预留只是账面占用，油品仍在罐内，谱系池按物理位置计算；
    # 预留与隔离的重叠在立案/预留事务中单独拒绝。
    tank = quantize_volume(available - open_isolation_barrels(connection, lot_id))
    blend_edges: list[tuple[str, Decimal]] = [
        (row["output_lot_id"], Decimal(row["quantity_barrels"])) for row in blends
    ]
    transfer_edges: list[tuple[str, Decimal]] = [
        (row["transfer_id"], Decimal(row["loaded_barrels"])) for row in transfers
    ]
    base = quantize_volume(
        tank + sum((q for _, q in blend_edges), ZERO) + sum((q for _, q in transfer_edges), ZERO)
    )
    return {
        "tank": tank,
        "blend_edges": blend_edges,
        "transfer_edges": transfer_edges,
        "base": base,
        "quantity": Decimal(lot["quantity_barrels"]),
    }


def _topological_order(connection: sqlite3.Connection, root_lot_id: str) -> list[str]:
    """从根批次沿混兑边向前的拓扑顺序（混兑产出批次总是晚于配料批次）。"""
    order: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(lot_id: str) -> None:
        if lot_id in seen:
            return
        if lot_id in visiting:  # pragma: no cover - 混兑只产生新批次，不应成环
            raise InvalidState("混兑谱系出现环，无法传播隔离")
        visiting.add(lot_id)
        rows = connection.execute(
            "SELECT b.output_lot_id FROM blend_ingredients bi "
            "JOIN blends b ON b.blend_id=bi.blend_id WHERE bi.input_lot_id=?",
            (lot_id,),
        ).fetchall()
        for row in rows:
            visit(row["output_lot_id"])
        visiting.discard(lot_id)
        seen.add(lot_id)
        order.append(lot_id)

    visit(root_lot_id)
    order.reverse()
    return order


def propagate_impact(
    connection: sqlite3.Connection, root_lot_id: str, affected: Decimal
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """把受影响数量沿谱系分配到罐内批次和在途转运。

    返回 ``(lot_tanks, transfers)``：前者是各批次仍留在罐内的受影响数量，
    后者是各在途转运上的受影响数量。每个批次按罐内、混兑配料、在途转运
    的实际数量比例分摊；分摊尾差归罐内份额，保证总量守恒。混兑产出批次
    汇总所有配料传入的份额后再继续向下传播。
    """
    affected = quantize_volume(affected)
    if affected <= ZERO:
        raise InvalidState("受影响数量必须为正数")
    root_pool = _lot_pool(connection, root_lot_id)
    if affected > root_pool["base"]:
        raise InvalidState(
            f"受影响数量 {decimal_text(affected)} 超过该批次仍可追溯的罐内与在途数量 "
            f"{decimal_text(root_pool['base'])}"
        )

    inflow: dict[str, Decimal] = {root_lot_id: affected}
    transfer_load: dict[str, Decimal] = {}
    lot_tanks: dict[str, Decimal] = {}
    for lot_id in _topological_order(connection, root_lot_id):
        quantity = inflow.get(lot_id, ZERO)
        if quantity <= ZERO:
            continue
        pool = _lot_pool(connection, lot_id)
        base = pool["base"]  # type: ignore[index]
        if quantity > base:
            raise InvalidState(
                f"批次 {lot_id} 受影响数量 {decimal_text(quantity)} 超过可追溯数量 "
                f"{decimal_text(base)}"
            )
        allocated = ZERO
        for output_lot_id, edge_quantity in pool["blend_edges"]:  # type: ignore[index]
            share = quantize_volume(quantity * edge_quantity / base)
            allocated += share
            inflow[output_lot_id] = inflow.get(output_lot_id, ZERO) + share
        for transfer_id, edge_quantity in pool["transfer_edges"]:  # type: ignore[index]
            share = quantize_volume(quantity * edge_quantity / base)
            allocated += share
            transfer_load[transfer_id] = transfer_load.get(transfer_id, ZERO) + share
        tank_share = quantize_volume(quantity - allocated)
        if tank_share > ZERO:
            lot_tanks[lot_id] = tank_share
    return lot_tanks, {key: quantize_volume(value) for key, value in transfer_load.items()}
