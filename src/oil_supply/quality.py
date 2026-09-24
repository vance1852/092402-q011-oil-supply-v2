"""质量样品、检测版本、混兑谱系与数量隔离用例。

本模块以混入方式扩展 ``SupplyService``，复用其权限、时钟与哈希审计能力：

- 检测结果只能追加新版本，草稿经两名与记录人不同的人员确认后生效；
- 混兑按配料实际数量建立谱系边，并即时扣减原料批次的可用量；
- 隔离案件沿罐内与在途的实际数量谱系传播，同罐无关批次不受影响；
- 解除隔离必须逐目标给出处置数量，处置合计与隔离数量严格相等；
- 在途转运只做待处置标记，不回滚已经离开储罐的历史发运。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .genealogy import (
    held_reservation_barrels,
    lot_free_barrels,
    open_isolation_barrels,
    propagate_impact,
)
from .models import (
    BlendRecord,
    DispositionInstruction,
    InventoryReservation,
    IsolationCase,
    QualitySample,
    QualityTestVersion,
)
from .planning import decimal_text, quantize_volume
from .storage import transaction

ZERO = Decimal("0")
TRANSFER_DISPOSABLE_ACTIONS = {"release", "destroy"}


class QualityOperations:
    # 以下属性由 SupplyService 提供
    connection: sqlite3.Connection

    def register_quality_sample(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "sample.write")
        sample = QualitySample.from_dict(raw)
        lot = self.connection.execute(
            "SELECT 1 FROM inventory_lots WHERE lot_id=?", (sample.lot_id,)
        ).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_samples(sample_id,lot_id,sampled_quantity_barrels,sampled_at,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        sample.sample_id,
                        sample.lot_id,
                        decimal_text(sample.sampled_quantity_barrels),
                        sample.sampled_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("quality_sample", sample.sample_id, "sample.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("样品编号冲突或批次不存在") from exc
        return {
            "sample_id": sample.sample_id,
            "lot_id": sample.lot_id,
            "sampled_quantity_barrels": decimal_text(sample.sampled_quantity_barrels),
            "state": "registered",
        }

    def record_test_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "test.write")
        test = QualityTestVersion.from_dict(raw)
        sample = self.connection.execute(
            "SELECT sample_id FROM quality_samples WHERE sample_id=?", (test.sample_id,)
        ).fetchone()
        if sample is None:
            raise NotFound("样品不存在")
        conclusion = (
            test.conclusion_override
            if test.conclusion_override is not None
            else "pass"
            if test.spec_min <= test.measured_value <= test.spec_max
            else "fail"
        )
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version_no),0) AS version_no "
                "FROM quality_test_versions WHERE sample_id=?",
                (test.sample_id,),
            ).fetchone()
            version_no = int(row["version_no"]) + 1
            cursor = self.connection.execute(
                "INSERT INTO quality_test_versions(sample_id,version_no,test_code,measured_value,"
                "spec_min,spec_max,conclusion,method,instrument_id,state,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    test.sample_id,
                    version_no,
                    test.test_code,
                    decimal_text(test.measured_value),
                    decimal_text(test.spec_min),
                    decimal_text(test.spec_max),
                    conclusion,
                    test.method,
                    test.instrument_id,
                    "draft",
                    actor_id,
                    self._now(),
                ),
            )
            test_version_id = int(cursor.lastrowid)
            self._audit(
                "quality_test",
                str(test_version_id),
                "test.recorded",
                actor_id,
                {"sample_id": test.sample_id, "version_no": version_no, "conclusion": conclusion},
            )
        return {
            "test_version_id": test_version_id,
            "sample_id": test.sample_id,
            "version_no": version_no,
            "conclusion": conclusion,
            "state": "draft",
        }

    def confirm_test_version(
        self, actor_id: str, test_version_id: int, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "test.confirm")
        if isinstance(test_version_id, bool) or not isinstance(test_version_id, int) or test_version_id <= 0:
            raise ValidationFailed("test_version_id 必须是正整数")
        note = (note or "").strip()
        if len(note) > 512:
            raise ValidationFailed("备注不能超过 512 个字符")
        with transaction(self.connection, immediate=True):
            version = self.connection.execute(
                "SELECT * FROM quality_test_versions WHERE test_version_id=?",
                (test_version_id,),
            ).fetchone()
            if version is None:
                raise NotFound("检测版本不存在")
            if version["state"] != "draft":
                raise InvalidState("只有草稿检测版本可以确认")
            if version["recorded_by"] == actor_id:
                raise Conflict("检测记录人不能确认自己的检测结论")
            existing = self.connection.execute(
                "SELECT confirmed_by FROM quality_test_confirmations WHERE test_version_id=?",
                (test_version_id,),
            ).fetchall()
            confirmers = {row["confirmed_by"] for row in existing}
            if actor_id in confirmers:
                raise Conflict("该人员已经确认过此检测版本")
            if len(confirmers) >= 2:  # pragma: no cover - 草稿至多两次确认即生效
                raise InvalidState("检测版本已集齐确认")
            self.connection.execute(
                "INSERT INTO quality_test_confirmations(test_version_id,confirmed_by,confirmed_at,note) "
                "VALUES(?,?,?,?)",
                (test_version_id, actor_id, self._now(), note),
            )
            confirmers.add(actor_id)
            state = "draft"
            if len(confirmers) >= 2:
                self.connection.execute(
                    "UPDATE quality_test_versions SET state='confirmed' WHERE test_version_id=? AND state='draft'",
                    (test_version_id,),
                )
                state = "confirmed"
            self._audit(
                "quality_test",
                str(test_version_id),
                "test.confirmed",
                actor_id,
                {"state": state, "confirmations": len(confirmers)},
            )
        return {"test_version_id": test_version_id, "state": state, "confirmations": len(confirmers)}

    def record_blend(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "blend.write")
        blend = BlendRecord.from_dict(raw)
        ingredient_total = quantize_volume(
            sum((item.quantity_barrels for item in blend.ingredients), ZERO)
        )
        if ingredient_total != blend.output_quantity_barrels:
            raise ValidationFailed(
                f"混兑产出 {decimal_text(blend.output_quantity_barrels)} 必须等于配料合计 "
                f"{decimal_text(ingredient_total)}"
            )
        facility = self.connection.execute(
            "SELECT facility_id FROM facilities WHERE facility_id=?", (blend.facility_id,)
        ).fetchone()
        if facility is None:
            raise NotFound("设施不存在")
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM inventory_lots WHERE lot_id=?", (blend.output_lot_id,)
            ).fetchone() is not None:
                raise Conflict("产出批次编号已经存在")
            unit_cost_total = ZERO
            for ingredient in blend.ingredients:
                lot = self.connection.execute(
                    "SELECT * FROM inventory_lots WHERE lot_id=?", (ingredient.lot_id,)
                ).fetchone()
                if lot is None:
                    raise NotFound(f"原料批次 {ingredient.lot_id} 不存在")
                if lot["facility_id"] != blend.facility_id:
                    raise Conflict(f"原料批次 {ingredient.lot_id} 不在混兑设施内")
                if lot["product"] != blend.product:
                    raise Conflict(f"原料批次 {ingredient.lot_id} 油品与混兑产出不一致")
                free = lot_free_barrels(self.connection, ingredient.lot_id)
                if free < ingredient.quantity_barrels:
                    raise Conflict(
                        f"原料批次 {ingredient.lot_id} 可混兑数量不足 "
                        f"(可用 {decimal_text(free)})"
                    )
                unit_cost_total += ingredient.quantity_barrels * Decimal(lot["unit_cost_usd"])
            average_cost = (
                unit_cost_total / blend.output_quantity_barrels
                if blend.output_quantity_barrels
                else ZERO
            )
            for ingredient in blend.ingredients:
                lot = self.connection.execute(
                    "SELECT available_barrels,quantity_barrels FROM inventory_lots WHERE lot_id=?",
                    (ingredient.lot_id,),
                ).fetchone()
                self.connection.execute(
                    "UPDATE inventory_lots SET available_barrels=?,quantity_barrels=?,revision=revision+1 "
                    "WHERE lot_id=?",
                    (
                        decimal_text(quantize_volume(Decimal(lot["available_barrels"]) - ingredient.quantity_barrels)),
                        decimal_text(quantize_volume(Decimal(lot["quantity_barrels"]) - ingredient.quantity_barrels)),
                        ingredient.lot_id,
                    ),
                )
            self.connection.execute(
                "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    blend.output_lot_id,
                    blend.facility_id,
                    blend.product,
                    blend.grade,
                    decimal_text(blend.output_quantity_barrels),
                    decimal_text(blend.output_quantity_barrels),
                    decimal_text(average_cost),
                    blend.occurred_at,
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.execute(
                "INSERT INTO blends(blend_id,output_lot_id,facility_id,product,grade,"
                "output_quantity_barrels,occurred_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    blend.blend_id,
                    blend.output_lot_id,
                    blend.facility_id,
                    blend.product,
                    blend.grade,
                    decimal_text(blend.output_quantity_barrels),
                    blend.occurred_at,
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.executemany(
                "INSERT INTO blend_ingredients(blend_id,input_lot_id,quantity_barrels) VALUES(?,?,?)",
                [
                    (blend.blend_id, item.lot_id, decimal_text(item.quantity_barrels))
                    for item in blend.ingredients
                ],
            )
            self._audit(
                "blend",
                blend.blend_id,
                "blend.recorded",
                actor_id,
                {"output_lot_id": blend.output_lot_id, "ingredients": len(blend.ingredients)},
            )
        return {
            "blend_id": blend.blend_id,
            "output_lot_id": blend.output_lot_id,
            "output_quantity_barrels": decimal_text(blend.output_quantity_barrels),
            "unit_cost_usd": decimal_text(average_cost),
        }

    def create_reservation(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        reservation = InventoryReservation.from_dict(raw)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT * FROM inventory_reservations WHERE idempotency_key=?",
                (reservation.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["reservation_id"] != reservation.reservation_id:
                    raise Conflict("幂等键对应不同预留")
                return {
                    "reservation_id": existing["reservation_id"],
                    "lot_id": existing["lot_id"],
                    "quantity_barrels": existing["quantity_barrels"],
                    "state": existing["state"],
                    "replayed": True,
                }
            lot = self.connection.execute(
                "SELECT 1 FROM inventory_lots WHERE lot_id=?", (reservation.lot_id,)
            ).fetchone()
            if lot is None:
                raise NotFound("库存批次不存在")
            free = lot_free_barrels(self.connection, reservation.lot_id)
            if free < reservation.quantity_barrels:
                isolated = open_isolation_barrels(self.connection, reservation.lot_id)
                held = held_reservation_barrels(self.connection, reservation.lot_id)
                raise Conflict(
                    f"可预留数量不足：空闲 {decimal_text(free)}，已隔离 {decimal_text(isolated)}，"
                    f"已预留 {decimal_text(held)}"
                )
            self.connection.execute(
                "INSERT INTO inventory_reservations(reservation_id,lot_id,quantity_barrels,"
                "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (
                    reservation.reservation_id,
                    reservation.lot_id,
                    decimal_text(reservation.quantity_barrels),
                    reservation.idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            self._audit(
                "reservation",
                reservation.reservation_id,
                "reservation.held",
                actor_id,
                {"lot_id": reservation.lot_id, "quantity_barrels": decimal_text(reservation.quantity_barrels)},
            )
        return {
            "reservation_id": reservation.reservation_id,
            "lot_id": reservation.lot_id,
            "quantity_barrels": decimal_text(reservation.quantity_barrels),
            "state": "held",
            "replayed": False,
        }

    def release_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE inventory_reservations SET state='released' "
                "WHERE reservation_id=? AND state='held'",
                (reservation_id,),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    "SELECT state FROM inventory_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise NotFound("预留不存在")
                raise InvalidState(f"预留当前状态为 {row['state']}，不能释放")
            self._audit("reservation", reservation_id, "reservation.released", actor_id, {})
        return {"reservation_id": reservation_id, "state": "released"}

    def _latest_confirmed_failure(self, sample_id: str) -> sqlite3.Row:
        version = self.connection.execute(
            "SELECT * FROM quality_test_versions WHERE sample_id=? AND state='confirmed' "
            "ORDER BY version_no DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        if version is None:
            raise InvalidState("隔离必须基于经两名不同人员确认的检测版本")
        if version["conclusion"] not in {"fail", "inconclusive"}:
            raise InvalidState("只有不合格或存疑的检测结论可以立案隔离")
        return version

    def open_isolation_case(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "case.write")
        case = IsolationCase.from_dict(raw)
        sample = self.connection.execute(
            "SELECT * FROM quality_samples WHERE sample_id=?", (case.sample_id,)
        ).fetchone()
        if sample is None:
            raise NotFound("样品不存在")
        duplicate = self.connection.execute(
            "SELECT case_id FROM isolation_cases WHERE sample_id=? AND state='open'",
            (case.sample_id,),
        ).fetchone()
        if duplicate is not None:
            raise Conflict(f"样品已有未结隔离案件 {duplicate['case_id']}")
        version = self._latest_confirmed_failure(case.sample_id)
        affected = quantize_volume(
            case.affected_quantity_barrels
            if case.affected_quantity_barrels is not None
            else Decimal(sample["sampled_quantity_barrels"])
        )
        with transaction(self.connection, immediate=True):
            lot_tanks, transfer_loads = propagate_impact(
                self.connection, sample["lot_id"], affected
            )
            for lot_id, isolated_qty in lot_tanks.items():
                free = lot_free_barrels(self.connection, lot_id)
                if isolated_qty > free:
                    raise Conflict(
                        f"批次 {lot_id} 上的未消费预留占用了受影响数量：可隔离罐内数量仅 "
                        f"{decimal_text(free)}，需先释放或消费相关预留"
                    )
            self.connection.execute(
                "INSERT INTO isolation_cases(case_id,sample_id,root_lot_id,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (case.case_id, case.sample_id, sample["lot_id"], case.reason, actor_id, self._now()),
            )
            targets: list[dict[str, Any]] = []
            for lot_id in lot_tanks:
                cursor = self.connection.execute(
                    "INSERT INTO isolation_targets(case_id,scope,lot_id,isolated_barrels) "
                    "VALUES(?, 'lot', ?, ?)",
                    (case.case_id, lot_id, decimal_text(lot_tanks[lot_id])),
                )
                targets.append(
                    {
                        "target_id": int(cursor.lastrowid),
                        "scope": "lot",
                        "lot_id": lot_id,
                        "isolated_barrels": decimal_text(lot_tanks[lot_id]),
                    }
                )
            for transfer_id in sorted(transfer_loads):
                cursor = self.connection.execute(
                    "INSERT INTO isolation_targets(case_id,scope,transfer_id,isolated_barrels,transit_mark) "
                    "VALUES(?, 'transfer', ?, ?, 'held')",
                    (case.case_id, transfer_id, decimal_text(transfer_loads[transfer_id])),
                )
                self.connection.execute(
                    "UPDATE transfers SET state='held',revision=revision+1 "
                    "WHERE transfer_id=? AND state IN ('in_transit','held')",
                    (transfer_id,),
                )
                targets.append(
                    {
                        "target_id": int(cursor.lastrowid),
                        "scope": "transfer",
                        "transfer_id": transfer_id,
                        "isolated_barrels": decimal_text(transfer_loads[transfer_id]),
                        "transit_mark": "held",
                    }
                )
            self._audit(
                "isolation_case",
                case.case_id,
                "case.opened",
                actor_id,
                {
                    "sample_id": case.sample_id,
                    "root_lot_id": sample["lot_id"],
                    "affected_barrels": decimal_text(affected),
                    "test_version_id": version["test_version_id"],
                    "targets": len(targets),
                },
            )
        return {
            "case_id": case.case_id,
            "state": "open",
            "affected_barrels": decimal_text(affected),
            "root_lot_id": sample["lot_id"],
            "test_version_id": version["test_version_id"],
            "targets": targets,
        }

    def isolation_case(self, case_id: str) -> dict[str, Any]:
        case = self.connection.execute(
            "SELECT * FROM isolation_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFound("隔离案件不存在")
        return self._case_detail(case)

    def _case_detail(self, case: sqlite3.Row) -> dict[str, Any]:
        targets = self.connection.execute(
            "SELECT * FROM isolation_targets WHERE case_id=? ORDER BY target_id",
            (case["case_id"],),
        ).fetchall()
        target_rows = []
        for target in targets:
            dispositions = self.connection.execute(
                "SELECT action,quantity_barrels,note,acted_by,acted_at "
                "FROM isolation_dispositions WHERE target_id=? ORDER BY disposition_id",
                (target["target_id"],),
            ).fetchall()
            target_rows.append(
                {
                    "target_id": target["target_id"],
                    "scope": target["scope"],
                    "lot_id": target["lot_id"],
                    "transfer_id": target["transfer_id"],
                    "isolated_barrels": target["isolated_barrels"],
                    "transit_mark": target["transit_mark"],
                    "disposition_status": target["disposition_status"],
                    "disposition_barrels": target["disposition_barrels"],
                    "dispositions": [dict(row) for row in dispositions],
                }
            )
        versions = self.connection.execute(
            "SELECT v.* FROM quality_test_versions v JOIN quality_samples s ON s.sample_id=v.sample_id "
            "WHERE s.sample_id=? ORDER BY v.version_no",
            (case["sample_id"],),
        ).fetchall()
        version_rows = []
        for version in versions:
            confirmations = self.connection.execute(
                "SELECT confirmed_by,confirmed_at,note FROM quality_test_confirmations "
                "WHERE test_version_id=? ORDER BY rowid",
                (version["test_version_id"],),
            ).fetchall()
            version_rows.append({**dict(version), "confirmations": [dict(row) for row in confirmations]})
        return {
            "case_id": case["case_id"],
            "sample_id": case["sample_id"],
            "root_lot_id": case["root_lot_id"],
            "reason": case["reason"],
            "state": case["state"],
            "revision": case["revision"],
            "created_by": case["created_by"],
            "created_at": case["created_at"],
            "released_by": case["released_by"],
            "released_at": case["released_at"],
            "release_note": case["release_note"],
            "test_versions": version_rows,
            "targets": target_rows,
        }

    def release_isolation_case(
        self, actor_id: str, case_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "case.release")
        note = str(raw.get("note", "")).strip()
        if not note:
            raise ValidationFailed("解除隔离必须说明处置依据")
        if len(note) > 512:
            raise ValidationFailed("处置说明不能超过 512 个字符")
        instructions_raw = raw.get("dispositions")
        if not isinstance(instructions_raw, list) or not instructions_raw:
            raise ValidationFailed("解除隔离必须给出每项目标的处置数量")
        instructions = [DispositionInstruction.from_dict(item) for item in instructions_raw]
        instructions = [
            DispositionInstruction(
                target_id=item.target_id,
                action=item.action,
                quantity_barrels=quantize_volume(item.quantity_barrels),
                note=item.note,
                destination_lot_id=item.destination_lot_id,
                product=item.product,
                grade=item.grade,
            )
            for item in instructions
        ]
        with transaction(self.connection, immediate=True):
            case = self.connection.execute(
                "SELECT * FROM isolation_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if case is None:
                raise NotFound("隔离案件不存在")
            if case["state"] != "open":
                raise InvalidState("隔离案件已经解除")
            targets = {
                row["target_id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM isolation_targets WHERE case_id=?", (case_id,)
                ).fetchall()
            }
            grouped: dict[int, list[DispositionInstruction]] = {}
            for instruction in instructions:
                if instruction.target_id not in targets:
                    raise ValidationFailed(
                        f"target_id {instruction.target_id} 不属于案件 {case_id}"
                    )
                grouped.setdefault(instruction.target_id, []).append(instruction)
            if set(grouped) != set(targets):
                missing = sorted(set(targets) - set(grouped))
                raise ValidationFailed(f"目标 {missing} 缺少处置数量，不能解除隔离")
            applied: list[dict[str, Any]] = []
            for target_id, items in grouped.items():
                target = targets[target_id]
                isolated = Decimal(target["isolated_barrels"])
                total = quantize_volume(
                    sum((item.quantity_barrels for item in items), ZERO)
                )
                if total != isolated:
                    raise ValidationFailed(
                        f"目标 {target_id} 处置合计 {decimal_text(total)} 与隔离数量 "
                        f"{decimal_text(isolated)} 不相等，质量不守恒"
                    )
                if target["scope"] == "transfer":
                    for item in items:
                        if item.action not in TRANSFER_DISPOSABLE_ACTIONS:
                            raise ValidationFailed(
                                "在途转运只能按到货放行或销毁处置，不能回滚为新罐内批次"
                            )
                for item in items:
                    if target["scope"] == "lot":
                        self._apply_lot_disposition(target, item, actor_id)
                    self.connection.execute(
                        "INSERT INTO isolation_dispositions(target_id,action,quantity_barrels,note,"
                        "acted_by,acted_at) VALUES(?,?,?,?,?,?)",
                        (
                            target_id,
                            item.action,
                            decimal_text(item.quantity_barrels),
                            item.note,
                            actor_id,
                            self._now(),
                        ),
                    )
                    applied.append(
                        {
                            "target_id": target_id,
                            "scope": target["scope"],
                            "action": item.action,
                            "quantity_barrels": decimal_text(item.quantity_barrels),
                        }
                    )
                self.connection.execute(
                    "UPDATE isolation_targets SET disposition_status='disposed',"
                    "disposition_barrels=?,transit_mark=CASE WHEN scope='transfer' THEN 'disposed' "
                    "ELSE transit_mark END WHERE target_id=?",
                    (decimal_text(total), target_id),
                )
                if target["scope"] == "transfer":
                    actions = {item.action for item in items}
                    if actions == {"release"}:
                        other_open = self.connection.execute(
                            "SELECT 1 FROM isolation_targets ot JOIN isolation_cases oc "
                            "ON oc.case_id=ot.case_id WHERE ot.scope='transfer' "
                            "AND ot.transfer_id=? AND ot.target_id<>? AND oc.state='open' "
                            "AND ot.disposition_status='pending' LIMIT 1",
                            (target["transfer_id"], target_id),
                        ).fetchone()
                        if other_open is None:
                            self.connection.execute(
                                "UPDATE transfers SET state='in_transit',revision=revision+1 "
                                "WHERE transfer_id=? AND state='held'",
                                (target["transfer_id"],),
                            )
            self.connection.execute(
                "UPDATE isolation_cases SET state='released',revision=revision+1,"
                "released_by=?,released_at=?,release_note=? WHERE case_id=? AND state='open'",
                (actor_id, self._now(), note, case_id),
            )
            self._audit(
                "isolation_case",
                case_id,
                "case.released",
                actor_id,
                {"note": note, "dispositions": applied},
            )
        return {
            "case_id": case_id,
            "state": "released",
            "disposed_barrels": decimal_text(
                sum((Decimal(item["quantity_barrels"]) for item in applied), ZERO)
            ),
            "dispositions": applied,
        }

    def _apply_lot_disposition(
        self, target: sqlite3.Row, instruction: DispositionInstruction, actor_id: str
    ) -> None:
        lot = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE lot_id=?", (target["lot_id"],)
        ).fetchone()
        quantity = instruction.quantity_barrels
        if instruction.action == "release":
            return
        available = Decimal(lot["available_barrels"])
        total_quantity = Decimal(lot["quantity_barrels"])
        if quantity > available:
            raise ValidationFailed(
                f"批次 {lot['lot_id']} 可处置罐内数量不足 {decimal_text(quantity)}"
            )
        if instruction.action == "destroy":
            self.connection.execute(
                "UPDATE inventory_lots SET quantity_barrels=?,available_barrels=?,revision=revision+1 "
                "WHERE lot_id=?",
                (
                    decimal_text(quantize_volume(total_quantity - quantity)),
                    decimal_text(quantize_volume(available - quantity)),
                    lot["lot_id"],
                ),
            )
            self._audit(
                "inventory_lot",
                lot["lot_id"],
                "inventory.destroyed",
                actor_id,
                {"quantity_barrels": decimal_text(quantity), "target_id": target["target_id"]},
            )
            return
        destination = instruction.destination_lot_id
        assert destination is not None and instruction.product is not None and instruction.grade is not None
        if self.connection.execute(
            "SELECT 1 FROM inventory_lots WHERE lot_id=?", (destination,)
        ).fetchone() is not None:
            raise Conflict(f"处置目标批次 {destination} 已经存在")
        self.connection.execute(
            "UPDATE inventory_lots SET quantity_barrels=?,available_barrels=?,revision=revision+1 "
            "WHERE lot_id=?",
            (
                decimal_text(quantize_volume(total_quantity - quantity)),
                decimal_text(quantize_volume(available - quantity)),
                lot["lot_id"],
            ),
        )
        self.connection.execute(
            "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
            "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                destination,
                lot["facility_id"],
                instruction.product,
                instruction.grade,
                decimal_text(quantize_volume(quantity)),
                decimal_text(quantize_volume(quantity)),
                lot["unit_cost_usd"],
                self._now(),
                actor_id,
                self._now(),
            ),
        )
        self.connection.execute(
            "INSERT INTO disposition_derived_lots(destination_lot_id,source_lot_id,target_id,"
            "action,created_at) VALUES(?,?,?,?,?)",
            (destination, lot["lot_id"], target["target_id"], instruction.action, self._now()),
        )
        self._audit(
            "inventory_lot",
            destination,
            f"inventory.{instruction.action}ed",
            actor_id,
            {
                "source_lot_id": lot["lot_id"],
                "quantity_barrels": decimal_text(quantity),
                "target_id": target["target_id"],
            },
        )

    def lot_trace(self, lot_id: str) -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        reserved = held_reservation_barrels(self.connection, lot_id)
        isolated = open_isolation_barrels(self.connection, lot_id)
        available = Decimal(lot["available_barrels"])
        free = lot_free_barrels(self.connection, lot_id)

        sources: list[dict[str, Any]] = []

        def collect_sources(current: str, depth: int) -> None:
            rows = self.connection.execute(
                "SELECT bi.blend_id,bi.input_lot_id,bi.quantity_barrels,b.occurred_at "
                "FROM blend_ingredients bi JOIN blends b ON b.blend_id=bi.blend_id "
                "WHERE b.output_lot_id=? ORDER BY bi.input_lot_id",
                (current,),
            ).fetchall()
            for row in rows:
                sources.append(
                    {
                        "lot_id": row["input_lot_id"],
                        "via": "blend",
                        "via_blend_id": row["blend_id"],
                        "quantity_barrels": row["quantity_barrels"],
                        "blended_at": row["occurred_at"],
                        "depth": depth,
                    }
                )
                collect_sources(row["input_lot_id"], depth + 1)
            derived = self.connection.execute(
                "SELECT d.source_lot_id,d.action,d.target_id,d.created_at "
                "FROM disposition_derived_lots d WHERE d.destination_lot_id=?",
                (current,),
            ).fetchall()
            for row in derived:
                sources.append(
                    {
                        "lot_id": row["source_lot_id"],
                        "via": row["action"],
                        "target_id": row["target_id"],
                        "created_at": row["created_at"],
                        "depth": depth,
                    }
                )
                collect_sources(row["source_lot_id"], depth + 1)

        collect_sources(lot_id, 1)

        downstream: list[dict[str, Any]] = []
        blend_rows = self.connection.execute(
            "SELECT b.blend_id,b.output_lot_id,bi.quantity_barrels "
            "FROM blend_ingredients bi JOIN blends b ON b.blend_id=bi.blend_id "
            "WHERE bi.input_lot_id=? ORDER BY b.occurred_at,b.blend_id",
            (lot_id,),
        ).fetchall()
        for row in blend_rows:
            downstream.append(
                {
                    "kind": "blend",
                    "blend_id": row["blend_id"],
                    "output_lot_id": row["output_lot_id"],
                    "quantity_barrels": row["quantity_barrels"],
                }
            )
        transfer_rows = self.connection.execute(
            "SELECT transfer_id,loaded_barrels,state,departed_at,arrived_at "
            "FROM transfers WHERE inventory_lot_id=? ORDER BY departed_at,transfer_id",
            (lot_id,),
        ).fetchall()
        for row in transfer_rows:
            downstream.append(
                {
                    "kind": "transfer",
                    "transfer_id": row["transfer_id"],
                    "loaded_barrels": row["loaded_barrels"],
                    "state": row["state"],
                    "departed_at": row["departed_at"],
                    "arrived_at": row["arrived_at"],
                }
            )
        derived_rows = self.connection.execute(
            "SELECT destination_lot_id,target_id,action,created_at "
            "FROM disposition_derived_lots WHERE source_lot_id=? ORDER BY created_at",
            (lot_id,),
        ).fetchall()
        for row in derived_rows:
            downstream.append(
                {
                    "kind": row["action"],
                    "destination_lot_id": row["destination_lot_id"],
                    "target_id": row["target_id"],
                    "created_at": row["created_at"],
                }
            )

        lot_quantity = Decimal(lot["quantity_barrels"])
        impact_rows = self.connection.execute(
            "SELECT t.target_id,t.case_id,t.isolated_barrels,c.state AS case_state,c.reason,"
            "c.root_lot_id,c.sample_id "
            "FROM isolation_targets t JOIN isolation_cases c ON c.case_id=t.case_id "
            "WHERE t.scope='lot' AND t.lot_id=? ORDER BY t.case_id",
            (lot_id,),
        ).fetchall()
        impacts = []
        cases = []
        seen_cases: set[str] = set()
        for row in impact_rows:
            isolated_qty = Decimal(row["isolated_barrels"])
            ratio = ZERO if lot_quantity == ZERO else isolated_qty / lot_quantity * Decimal("100")
            impacts.append(
                {
                    "case_id": row["case_id"],
                    "target_id": row["target_id"],
                    "case_state": row["case_state"],
                    "isolated_barrels": row["isolated_barrels"],
                    "impact_percent": decimal_text(ratio.quantize(Decimal("0.0001"))),
                    "root_lot_id": row["root_lot_id"],
                }
            )
            if row["case_id"] not in seen_cases:
                seen_cases.add(row["case_id"])
                full = self.connection.execute(
                    "SELECT * FROM isolation_cases WHERE case_id=?", (row["case_id"],)
                ).fetchone()
                cases.append(self._case_detail(full))

        transfer_impact_rows = self.connection.execute(
            "SELECT t.case_id,t.isolated_barrels,t.transit_mark,tr.state "
            "FROM isolation_targets t JOIN transfers tr ON tr.transfer_id=t.transfer_id "
            "WHERE t.scope='transfer' AND tr.inventory_lot_id=? ORDER BY t.case_id",
            (lot_id,),
        ).fetchall()

        return {
            "lot_id": lot_id,
            "facility_id": lot["facility_id"],
            "product": lot["product"],
            "grade": lot["grade"],
            "quantity_barrels": lot["quantity_barrels"],
            "available_barrels": lot["available_barrels"],
            "reserved_held_barrels": decimal_text(reserved),
            "isolated_open_barrels": decimal_text(isolated),
            "free_barrels": decimal_text(free),
            "sources": sources,
            "downstream": downstream,
            "quality_impacts": impacts,
            "transfer_impacts": [dict(row) for row in transfer_impact_rows],
            "isolation_cases": cases,
            "decision_chain": self._decision_chain(lot_id, cases),
        }

    def _decision_chain(self, lot_id: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        for case in sorted(cases, key=lambda item: item["created_at"]):
            sample = self.connection.execute(
                "SELECT * FROM quality_samples WHERE sample_id=?", (case["sample_id"],)
            ).fetchone()
            chain.append(
                {
                    "at": sample["created_at"],
                    "ordinal": 10,
                    "type": "sample.registered",
                    "sample_id": sample["sample_id"],
                    "lot_id": sample["lot_id"],
                    "sampled_quantity_barrels": sample["sampled_quantity_barrels"],
                    "actor_id": sample["created_by"],
                }
            )
            for version_order, version in enumerate(case["test_versions"], start=1):
                chain.append(
                    {
                        "at": version["recorded_at"],
                        "ordinal": 20 + version_order,
                        "type": "test.recorded",
                        "sample_id": version["sample_id"],
                        "version_no": version["version_no"],
                        "test_version_id": version["test_version_id"],
                        "test_code": version["test_code"],
                        "measured_value": version["measured_value"],
                        "conclusion": version["conclusion"],
                        "state": version["state"],
                        "actor_id": version["recorded_by"],
                    }
                )
                for confirmation in version["confirmations"]:
                    chain.append(
                        {
                            "at": confirmation["confirmed_at"],
                            "ordinal": 40 + version_order,
                            "type": "test.confirmed",
                            "test_version_id": version["test_version_id"],
                            "version_no": version["version_no"],
                            "actor_id": confirmation["confirmed_by"],
                            "note": confirmation["note"],
                        }
                    )
            chain.append(
                {
                    "at": case["created_at"],
                    "ordinal": 60,
                    "type": "case.opened",
                    "case_id": case["case_id"],
                    "root_lot_id": case["root_lot_id"],
                    "reason": case["reason"],
                    "actor_id": case["created_by"],
                    "targets": [
                        {
                            "target_id": target["target_id"],
                            "scope": target["scope"],
                            "lot_id": target["lot_id"],
                            "transfer_id": target["transfer_id"],
                            "isolated_barrels": target["isolated_barrels"],
                            "transit_mark": target["transit_mark"],
                        }
                        for target in case["targets"]
                    ],
                }
            )
            if case["state"] == "released":
                chain.append(
                    {
                        "at": case["released_at"],
                        "ordinal": 70,
                        "type": "case.released",
                        "case_id": case["case_id"],
                        "note": case["release_note"],
                        "actor_id": case["released_by"],
                        "dispositions": [
                            {
                                "target_id": target["target_id"],
                                "scope": target["scope"],
                                "disposition_barrels": target["disposition_barrels"],
                                "actions": [
                                    {
                                        "action": row["action"],
                                        "quantity_barrels": row["quantity_barrels"],
                                        "actor_id": row["acted_by"],
                                        "at": row["acted_at"],
                                    }
                                    for row in target["dispositions"]
                                ],
                            }
                            for target in case["targets"]
                        ],
                    }
                )
        chain.sort(key=lambda item: (item["at"], item["ordinal"]))
        for item in chain:
            item.pop("ordinal", None)
        return chain
