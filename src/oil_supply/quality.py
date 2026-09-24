"""质检样品、检测版本、混兑谱系、隔离案件与库存预留。

隔离范围沿实际混兑数量谱系按比例传播：根批次视为全部可疑，下游批次
按各来源混入数量加权得到影响比例，持有数量在立案时冻结。发运、混兑和
库存预留都只能动用账面可用减去隔离持有与有效预留后的数量，同罐无关
批次不受影响。解除隔离必须逐批次说明处置数量并保持质量守恒。
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    PRODUCTS,
    decimal_value,
    identifier,
    optional_text,
    positive_integer,
    required_text,
)
from .planning import canonical_json, decimal_text, digest, quantize_volume
from .service import SupplyService
from .storage import transaction


ZERO = Decimal("0")
RATIO_PLACES = Decimal("0.000001")
COST_PLACES = Decimal("0.0001")

QUALITY_METRICS = {"octane_ron", "octane_mon", "density", "sulfur", "vapor_pressure", "distillation"}
SAMPLE_KINDS = {"routine", "retest", "investigation"}
TEST_CONCLUSIONS = {"pass", "fail", "inconclusive"}
DISPOSITION_ACTIONS = {"release", "downgrade", "destroy"}


def _utc_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parsed = parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return text if text.endswith("Z") else parsed.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SampleInput:
    sample_id: str
    lot_id: str
    sample_kind: str
    drawn_at: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SampleInput":
        kind = required_text(raw.get("sample_kind", "routine"), "sample_kind", 24)
        if kind not in SAMPLE_KINDS:
            raise ValidationFailed("sample_kind 不是受支持的样品类型")
        return cls(
            sample_id=identifier(raw.get("sample_id"), "sample_id"),
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            sample_kind=kind,
            drawn_at=_utc_text(raw.get("drawn_at"), "drawn_at"),
            note=optional_text(raw.get("note"), "note"),
        )


@dataclass(frozen=True, slots=True)
class TestInput:
    sample_id: str
    metric: str
    result_value: Decimal
    conclusion: str
    tested_at: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TestInput":
        metric = required_text(raw.get("metric"), "metric", 32)
        if metric not in QUALITY_METRICS:
            raise ValidationFailed("metric 不是受支持的检测项目")
        conclusion = required_text(raw.get("conclusion"), "conclusion", 16)
        if conclusion not in TEST_CONCLUSIONS:
            raise ValidationFailed("conclusion 必须是 pass、fail 或 inconclusive")
        return cls(
            sample_id=identifier(raw.get("sample_id"), "sample_id"),
            metric=metric,
            result_value=decimal_value(raw.get("result_value"), "result_value"),
            conclusion=conclusion,
            tested_at=_utc_text(raw.get("tested_at"), "tested_at"),
            note=optional_text(raw.get("note"), "note"),
        )


@dataclass(frozen=True, slots=True)
class BlendLine:
    source_lot_id: str
    quantity_barrels: Decimal


@dataclass(frozen=True, slots=True)
class BlendInput:
    blend_id: str
    target_lot_id: str
    facility_id: str
    product: str
    grade: str
    lines: tuple[BlendLine, ...]
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BlendInput":
        target = raw.get("target")
        if not isinstance(target, Mapping):
            raise ValidationFailed("target 必须是对象")
        product = required_text(target.get("product"), "target.product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("target.product 不是受支持的油品")
        lines_raw = raw.get("lines")
        if not isinstance(lines_raw, Sequence) or isinstance(lines_raw, (str, bytes)) or not lines_raw:
            raise ValidationFailed("lines 必须是非空数组")
        lines: list[BlendLine] = []
        seen: set[str] = set()
        for index, item in enumerate(lines_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed("lines 元素必须是对象")
            source = identifier(item.get("source_lot_id"), f"lines[{index}].source_lot_id")
            if source in seen:
                raise ValidationFailed("lines 中来源批次重复")
            seen.add(source)
            lines.append(BlendLine(
                source,
                decimal_value(item.get("quantity_barrels"), f"lines[{index}].quantity_barrels", minimum=Decimal("0.001")),
            ))
        return cls(
            blend_id=identifier(raw.get("blend_id"), "blend_id"),
            target_lot_id=identifier(target.get("lot_id"), "target.lot_id"),
            facility_id=identifier(target.get("facility_id"), "target.facility_id"),
            product=product,
            grade=required_text(target.get("grade"), "target.grade", 32).upper(),
            lines=tuple(lines),
            note=optional_text(raw.get("note"), "note"),
        )


@dataclass(frozen=True, slots=True)
class QuarantineCaseInput:
    case_id: str
    root_lot_id: str
    test_version_id: int
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QuarantineCaseInput":
        return cls(
            case_id=identifier(raw.get("case_id"), "case_id"),
            root_lot_id=identifier(raw.get("root_lot_id"), "root_lot_id"),
            test_version_id=positive_integer(raw.get("test_version_id"), "test_version_id"),
            reason=required_text(raw.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class DispositionInput:
    action: str
    quantity_barrels: Decimal


@dataclass(frozen=True, slots=True)
class ReleaseItemInput:
    lot_id: str
    dispositions: tuple[DispositionInput, ...]
    note: str


def _parse_release_items(raw: Mapping[str, Any]) -> tuple[ReleaseItemInput, ...]:
    items_raw = raw.get("items")
    if not isinstance(items_raw, Sequence) or isinstance(items_raw, (str, bytes)) or not items_raw:
        raise ValidationFailed("items 必须是非空数组")
    items: list[ReleaseItemInput] = []
    seen: set[str] = set()
    for index, item in enumerate(items_raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed("items 元素必须是对象")
        lot_id = identifier(item.get("lot_id"), f"items[{index}].lot_id")
        if lot_id in seen:
            raise ValidationFailed("items 中批次重复")
        seen.add(lot_id)
        dispositions_raw = item.get("dispositions")
        if not isinstance(dispositions_raw, Sequence) or isinstance(dispositions_raw, (str, bytes)):
            raise ValidationFailed("dispositions 必须是数组")
        dispositions: list[DispositionInput] = []
        for position, entry in enumerate(dispositions_raw):
            if not isinstance(entry, Mapping):
                raise ValidationFailed("dispositions 元素必须是对象")
            action = required_text(entry.get("action"), f"items[{index}].dispositions[{position}].action", 16)
            if action not in DISPOSITION_ACTIONS:
                raise ValidationFailed("action 必须是 release、downgrade 或 destroy")
            dispositions.append(DispositionInput(
                action,
                decimal_value(
                    entry.get("quantity_barrels"),
                    f"items[{index}].dispositions[{position}].quantity_barrels",
                    minimum=Decimal("0.001"),
                ),
            ))
        items.append(ReleaseItemInput(lot_id, tuple(dispositions), optional_text(item.get("note"), f"items[{index}].note")))
    return tuple(items)


@dataclass(frozen=True, slots=True)
class ReservationInput:
    reservation_id: str
    lot_id: str
    quantity_barrels: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationInput":
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            quantity_barrels=decimal_value(raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


class QualityService(SupplyService):
    """在供应调度服务之上扩展质检、谱系和隔离能力。"""

    # ---- 样品与检测版本 -------------------------------------------------

    def create_sample(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quality.write")
        sample = SampleInput.from_dict(raw)
        self.inventory_lot(sample.lot_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_samples(sample_id,lot_id,sample_kind,drawn_by,drawn_at,note,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (sample.sample_id, sample.lot_id, sample.sample_kind, actor_id, sample.drawn_at, sample.note, self._now()),
                )
                self._audit(
                    "quality_sample",
                    sample.sample_id,
                    "quality.sampled",
                    actor_id,
                    {"lot_id": sample.lot_id, "sample_kind": sample.sample_kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("样品编号已经存在") from exc
        return {"sample_id": sample.sample_id, "lot_id": sample.lot_id, "sample_kind": sample.sample_kind}

    def _sample(self, sample_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM quality_samples WHERE sample_id=?", (sample_id,)
        ).fetchone()
        if row is None:
            raise NotFound("样品不存在")
        return row

    def _test_version(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM quality_test_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("检测版本不存在")
        return row

    def record_test(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记新的检测结果；同一样品同一项目只产生后继版本，历史不覆盖。"""
        self._require(actor_id, "quality.write")
        test = TestInput.from_dict(raw)
        sample = self._sample(test.sample_id)
        previous = self.connection.execute(
            "SELECT version_id,version_no FROM quality_test_versions WHERE sample_id=? AND metric=? "
            "ORDER BY version_no DESC LIMIT 1",
            (test.sample_id, test.metric),
        ).fetchone()
        version_no = 1 if previous is None else int(previous["version_no"]) + 1
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO quality_test_versions(sample_id,metric,version_no,result_value,conclusion,state,"
                "supersedes_version_id,tested_by,tested_at,note) VALUES(?,?,?,?,?,'draft',?,?,?,?)",
                (
                    test.sample_id,
                    test.metric,
                    version_no,
                    decimal_text(test.result_value),
                    test.conclusion,
                    None if previous is None else previous["version_id"],
                    actor_id,
                    test.tested_at,
                    test.note,
                ),
            )
            version_id = int(cursor.lastrowid)
            self._audit(
                "quality_test",
                str(version_id),
                "quality.test_recorded",
                actor_id,
                {
                    "sample_id": test.sample_id,
                    "lot_id": sample["lot_id"],
                    "metric": test.metric,
                    "version_no": version_no,
                    "conclusion": test.conclusion,
                },
            )
        return {
            "version_id": version_id,
            "sample_id": test.sample_id,
            "metric": test.metric,
            "version_no": version_no,
            "state": "draft",
        }

    def confirm_test(self, actor_id: str, version_id: int) -> dict[str, Any]:
        """确认检测结论；复测结论必须由与检测人不同的人员确认。"""
        self._require(actor_id, "quality.confirm")
        version = self._test_version(version_id)
        if version["tested_by"] == actor_id:
            raise Forbidden("复测结论必须由不同人员确认")
        if version["state"] != "draft":
            raise InvalidState("检测版本已确认")
        latest = self.connection.execute(
            "SELECT MAX(version_no) AS latest FROM quality_test_versions WHERE sample_id=? AND metric=?",
            (version["sample_id"], version["metric"]),
        ).fetchone()
        if latest is not None and int(latest["latest"]) != int(version["version_no"]):
            raise InvalidState("只有最新检测版本可以确认")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE quality_test_versions SET state='confirmed',confirmed_by=?,confirmed_at=? "
                "WHERE version_id=? AND state='draft'",
                (actor_id, self._now(), version_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("检测版本已确认")
            self._audit(
                "quality_test",
                str(version_id),
                "quality.test_confirmed",
                actor_id,
                {"sample_id": version["sample_id"], "metric": version["metric"], "conclusion": version["conclusion"]},
            )
        return {"version_id": version_id, "state": "confirmed", "confirmed_by": actor_id}

    # ---- 混兑谱系 --------------------------------------------------------

    def record_blend(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记混兑：扣减来源批次可用量，按数量守恒生成目标批次。"""
        self._require(actor_id, "quality.write")
        blend = BlendInput.from_dict(raw)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                total = ZERO
                value = ZERO
                sources: list[tuple[BlendLine, sqlite3.Row]] = []
                for line in blend.lines:
                    if line.source_lot_id == blend.target_lot_id:
                        raise ValidationFailed("来源批次不能是目标批次")
                    lot = self.connection.execute(
                        "SELECT * FROM inventory_lots WHERE lot_id=?", (line.source_lot_id,)
                    ).fetchone()
                    if lot is None:
                        raise NotFound(f"来源批次 {line.source_lot_id} 不存在")
                    if self.usable_barrels(lot) < line.quantity_barrels:
                        raise Conflict("来源批次可动用数量不足，存在隔离持有或预留")
                    sources.append((line, lot))
                    total += line.quantity_barrels
                    value += line.quantity_barrels * Decimal(lot["unit_cost_usd"])
                unit_cost = (value / total).quantize(COST_PLACES, rounding=ROUND_HALF_UP)
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        blend.target_lot_id,
                        blend.facility_id,
                        blend.product,
                        blend.grade,
                        decimal_text(quantize_volume(total)),
                        decimal_text(quantize_volume(total)),
                        decimal_text(unit_cost),
                        now,
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO blend_events(blend_id,target_lot_id,blended_by,blended_at,note) VALUES(?,?,?,?,?)",
                    (blend.blend_id, blend.target_lot_id, actor_id, now, blend.note),
                )
                for line, lot in sources:
                    self.connection.execute(
                        "INSERT INTO blend_lines(blend_id,source_lot_id,quantity_barrels) VALUES(?,?,?)",
                        (blend.blend_id, line.source_lot_id, decimal_text(quantize_volume(line.quantity_barrels))),
                    )
                    remaining = quantize_volume(Decimal(lot["available_barrels"]) - line.quantity_barrels)
                    self.connection.execute(
                        "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=?",
                        (decimal_text(remaining), line.source_lot_id),
                    )
                self._audit(
                    "blend",
                    blend.blend_id,
                    "blend.recorded",
                    actor_id,
                    {
                        "target_lot_id": blend.target_lot_id,
                        "lines": [
                            {"source_lot_id": line.source_lot_id, "quantity_barrels": decimal_text(quantize_volume(line.quantity_barrels))}
                            for line, _ in sources
                        ],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("混兑编号或目标批次冲突，或设施不存在") from exc
        return self.inventory_lot(blend.target_lot_id)

    # ---- 隔离案件 --------------------------------------------------------

    def _propagate(self, root_lot_id: str) -> dict[str, Decimal]:
        """沿混兑谱系计算每个下游批次的疑似比例（根批次为 1）。"""
        adjacency: dict[str, list[tuple[str, Decimal]]] = {}
        rows = self.connection.execute(
            "SELECT bl.source_lot_id,bl.quantity_barrels,be.target_lot_id "
            "FROM blend_lines bl JOIN blend_events be ON be.blend_id=bl.blend_id"
        ).fetchall()
        for row in rows:
            adjacency.setdefault(row["source_lot_id"], []).append(
                (row["target_lot_id"], Decimal(row["quantity_barrels"]))
            )
        totals: dict[str, Decimal] = {}

        def target_total(lot_id: str) -> Decimal:
            if lot_id not in totals:
                row = self.connection.execute(
                    "SELECT quantity_barrels FROM inventory_lots WHERE lot_id=?", (lot_id,)
                ).fetchone()
                if row is None:
                    raise InvalidState("混兑谱系引用了不存在的批次")
                totals[lot_id] = Decimal(row["quantity_barrels"])
            return totals[lot_id]

        fractions = {root_lot_id: Decimal(1)}
        suspect_in: dict[str, Decimal] = {}
        queue: deque[str] = deque([root_lot_id])
        steps = 0
        while queue:
            steps += 1
            if steps > 100000:
                raise InvalidState("混兑谱系数据异常")
            current = queue.popleft()
            for target, quantity in adjacency.get(current, []):
                added = fractions[current] * quantity
                if added <= ZERO:
                    continue
                suspect_in[target] = suspect_in.get(target, ZERO) + added
                fraction = suspect_in[target] / target_total(target)
                if fractions.get(target) != fraction:
                    fractions[target] = fraction
                    queue.append(target)
        return fractions

    def open_quarantine(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """依据已确认的不合格检测立案，隔离范围沿数量谱系传播。"""
        self._require(actor_id, "quarantine.write")
        case = QuarantineCaseInput.from_dict(raw)
        version = self._test_version(case.test_version_id)
        if version["state"] != "confirmed":
            raise InvalidState("检测版本尚未确认，不能立案")
        if version["conclusion"] != "fail":
            raise ValidationFailed("只有不合格结论可以发起隔离")
        latest = self.connection.execute(
            "SELECT MAX(version_no) AS latest FROM quality_test_versions "
            "WHERE sample_id=? AND metric=? AND state='confirmed'",
            (version["sample_id"], version["metric"]),
        ).fetchone()
        if latest is not None and latest["latest"] is not None and int(latest["latest"]) != int(version["version_no"]):
            raise InvalidState("检测结论已被更新的确认版本取代")
        sample = self._sample(version["sample_id"])
        if sample["lot_id"] != case.root_lot_id:
            raise ValidationFailed("检测样品不属于该批次")
        root = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE lot_id=?", (case.root_lot_id,)
        ).fetchone()
        if root is None:
            raise NotFound("库存批次不存在")
        existing = self.connection.execute(
            "SELECT case_id FROM quarantine_cases WHERE root_lot_id=? AND status='open'",
            (case.root_lot_id,),
        ).fetchone()
        if existing is not None:
            raise Conflict("该批次已有未关闭的隔离案件")
        fractions = self._propagate(case.root_lot_id)
        now = self._now()
        items: list[dict[str, str]] = []
        flagged: list[str] = []
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quarantine_cases(case_id,root_lot_id,test_version_id,reason,opened_by,opened_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (case.case_id, case.root_lot_id, case.test_version_id, case.reason, actor_id, now),
                )
                for lot_id in sorted(fractions):
                    lot = self.connection.execute(
                        "SELECT available_barrels FROM inventory_lots WHERE lot_id=?", (lot_id,)
                    ).fetchone()
                    fraction = fractions[lot_id]
                    held = quantize_volume(fraction * Decimal(lot["available_barrels"]))
                    ratio = fraction.quantize(RATIO_PLACES, rounding=ROUND_HALF_UP)
                    self.connection.execute(
                        "INSERT INTO quarantine_items(case_id,lot_id,held_barrels,impact_ratio) VALUES(?,?,?,?)",
                        (case.case_id, lot_id, decimal_text(held), decimal_text(ratio)),
                    )
                    items.append({"lot_id": lot_id, "held_barrels": decimal_text(held), "impact_ratio": decimal_text(ratio)})
                placeholders = ",".join("?" for _ in fractions)
                transfers = self.connection.execute(
                    f"SELECT transfer_id FROM transfers WHERE state='in_transit' AND inventory_lot_id IN ({placeholders})",
                    tuple(sorted(fractions)),
                ).fetchall()
                for transfer in transfers:
                    self.connection.execute(
                        "INSERT INTO transfer_disposition_flags(transfer_id,case_id,flagged_at) VALUES(?,?,?)",
                        (transfer["transfer_id"], case.case_id, now),
                    )
                    flagged.append(transfer["transfer_id"])
                self._audit(
                    "quarantine",
                    case.case_id,
                    "quarantine.opened",
                    actor_id,
                    {
                        "root_lot_id": case.root_lot_id,
                        "test_version_id": case.test_version_id,
                        "items": items,
                        "flagged_transfers": flagged,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("隔离案件编号已经存在") from exc
        return {"case_id": case.case_id, "status": "open", "root_lot_id": case.root_lot_id, "items": items, "flagged_transfers": flagged}

    def _maybe_close_case(self, case_id: str, actor_id: str) -> None:
        remaining = self.connection.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE case_id=? AND state='held'", (case_id,)
        ).fetchone()["c"]
        pending = self.connection.execute(
            "SELECT COUNT(*) AS c FROM transfer_disposition_flags WHERE case_id=? AND status='pending_disposition'",
            (case_id,),
        ).fetchone()["c"]
        if remaining == 0 and pending == 0:
            cursor = self.connection.execute(
                "UPDATE quarantine_cases SET status='closed',closed_by=?,closed_at=? WHERE case_id=? AND status='open'",
                (actor_id, self._now(), case_id),
            )
            if cursor.rowcount == 1:
                self._audit("quarantine", case_id, "quarantine.closed", actor_id, {})

    def release_quarantine(self, actor_id: str, case_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """逐批次解除隔离；处置数量合计必须等于持有数量以保持质量守恒。"""
        self._require(actor_id, "quarantine.write")
        items = _parse_release_items(raw)
        case = self.connection.execute(
            "SELECT * FROM quarantine_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFound("隔离案件不存在")
        if case["status"] != "open":
            raise InvalidState("隔离案件已关闭")
        now = self._now()
        with transaction(self.connection, immediate=True):
            for item in items:
                held_row = self.connection.execute(
                    "SELECT * FROM quarantine_items WHERE case_id=? AND lot_id=?", (case_id, item.lot_id)
                ).fetchone()
                if held_row is None:
                    raise NotFound(f"批次 {item.lot_id} 不在隔离范围")
                if held_row["state"] != "held":
                    raise InvalidState(f"批次 {item.lot_id} 的隔离已解除")
                held = Decimal(held_row["held_barrels"])
                disposed = sum((entry.quantity_barrels for entry in item.dispositions), ZERO)
                if disposed != held:
                    raise ValidationFailed("处置数量合计必须等于隔离持有数量以保持质量守恒")
                lot = self.connection.execute(
                    "SELECT * FROM inventory_lots WHERE lot_id=?", (item.lot_id,)
                ).fetchone()
                available = Decimal(lot["available_barrels"])
                for position, entry in enumerate(item.dispositions):
                    self.connection.execute(
                        "INSERT INTO quarantine_dispositions(case_id,lot_id,action,quantity_barrels,note,actor_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (case_id, item.lot_id, entry.action, decimal_text(quantize_volume(entry.quantity_barrels)), item.note, actor_id, now),
                    )
                    if entry.action == "release":
                        continue
                    available = quantize_volume(available - entry.quantity_barrels)
                    if available < ZERO:
                        raise InvalidState("处置数量超过账面可用数量")
                    self.connection.execute(
                        "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=?",
                        (decimal_text(available), item.lot_id),
                    )
                    self.connection.execute(
                        "INSERT INTO inventory_adjustments(lot_id,delta_barrels,reason_code,note,idempotency_key,actor_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            item.lot_id,
                            decimal_text(quantize_volume(-entry.quantity_barrels)),
                            f"quarantine_{entry.action}",
                            item.note or case["reason"],
                            f"quarantine:{case_id}:{item.lot_id}:{position}",
                            actor_id,
                            now,
                        ),
                    )
                self.connection.execute(
                    "UPDATE quarantine_items SET state='released',released_at=? WHERE case_id=? AND lot_id=?",
                    (now, case_id, item.lot_id),
                )
                self._audit(
                    "quarantine",
                    case_id,
                    "quarantine.item_released",
                    actor_id,
                    {
                        "lot_id": item.lot_id,
                        "dispositions": [
                            {"action": entry.action, "quantity_barrels": decimal_text(quantize_volume(entry.quantity_barrels))}
                            for entry in item.dispositions
                        ],
                    },
                )
            self._maybe_close_case(case_id, actor_id)
        return self.quarantine_case(actor_id, case_id)

    def dispose_transfer(self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """处置在途转运标记；只追加处置结论，不回滚历史发运。"""
        self._require(actor_id, "quarantine.write")
        case_id = identifier(raw.get("case_id"), "case_id")
        note = required_text(raw.get("note"), "note")
        flag = self.connection.execute(
            "SELECT * FROM transfer_disposition_flags WHERE transfer_id=? AND case_id=?", (transfer_id, case_id)
        ).fetchone()
        if flag is None:
            raise NotFound("转运待处置标记不存在")
        if flag["status"] != "pending_disposition":
            raise InvalidState("转运已处置")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE transfer_disposition_flags SET status='disposed',disposed_by=?,disposed_at=?,disposition_note=? "
                "WHERE transfer_id=? AND case_id=?",
                (actor_id, self._now(), note, transfer_id, case_id),
            )
            self._audit(
                "transfer_flag",
                transfer_id,
                "transfer.disposed",
                actor_id,
                {"case_id": case_id, "note": note},
            )
            self._maybe_close_case(case_id, actor_id)
        return {"transfer_id": transfer_id, "case_id": case_id, "status": "disposed"}

    # ---- 库存预留 --------------------------------------------------------

    def reserve_inventory(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        reservation = ReservationInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='reservation' AND idempotency_key=?",
            (reservation.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同预留内容")
            return json.loads(stored["response_json"])
        lot = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE lot_id=?", (reservation.lot_id,)
        ).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        if self.usable_barrels(lot) < reservation.quantity_barrels:
            raise Conflict("库存可动用数量不足，存在隔离持有或预留")
        response = {
            "reservation_id": reservation.reservation_id,
            "lot_id": reservation.lot_id,
            "quantity_barrels": decimal_text(quantize_volume(reservation.quantity_barrels)),
            "state": "active",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_reservations(reservation_id,lot_id,quantity_barrels,state,idempotency_key,"
                    "created_by,created_at) VALUES(?,?,?,'active',?,?,?)",
                    (
                        reservation.reservation_id,
                        reservation.lot_id,
                        decimal_text(quantize_volume(reservation.quantity_barrels)),
                        reservation.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('reservation',?,?,?,?)",
                    (reservation.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "inventory_lot",
                    reservation.lot_id,
                    "inventory.reserved",
                    actor_id,
                    {"reservation_id": reservation.reservation_id, "quantity_barrels": response["quantity_barrels"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("预留编号或幂等键冲突") from exc
        return response

    def cancel_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        row = self.connection.execute(
            "SELECT * FROM inventory_reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预留不存在")
        if row["state"] != "active":
            raise InvalidState("预留不是有效状态")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_reservations SET state='cancelled' WHERE reservation_id=? AND state='active'",
                (reservation_id,),
            )
            self._audit(
                "inventory_lot",
                row["lot_id"],
                "inventory.reservation_cancelled",
                actor_id,
                {"reservation_id": reservation_id},
            )
        return {"reservation_id": reservation_id, "state": "cancelled"}

    # ---- 运营视图 --------------------------------------------------------

    def quarantine_case(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "quality.read")
        case = self.connection.execute(
            "SELECT * FROM quarantine_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFound("隔离案件不存在")
        items = self.connection.execute(
            "SELECT qi.*,il.product,il.grade,il.facility_id FROM quarantine_items qi "
            "JOIN inventory_lots il ON il.lot_id=qi.lot_id WHERE qi.case_id=? ORDER BY qi.lot_id",
            (case_id,),
        ).fetchall()
        dispositions = self.connection.execute(
            "SELECT * FROM quarantine_dispositions WHERE case_id=? ORDER BY disposition_id", (case_id,)
        ).fetchall()
        flags = self.connection.execute(
            "SELECT f.*,t.inventory_lot_id,t.loaded_barrels,t.state AS transfer_state "
            "FROM transfer_disposition_flags f JOIN transfers t ON t.transfer_id=f.transfer_id "
            "WHERE f.case_id=? ORDER BY f.transfer_id",
            (case_id,),
        ).fetchall()
        version = self._test_version(int(case["test_version_id"]))
        return {
            "case_id": case["case_id"],
            "root_lot_id": case["root_lot_id"],
            "reason": case["reason"],
            "status": case["status"],
            "opened_by": case["opened_by"],
            "opened_at": case["opened_at"],
            "closed_by": case["closed_by"],
            "closed_at": case["closed_at"],
            "test_version": {
                "version_id": version["version_id"],
                "sample_id": version["sample_id"],
                "metric": version["metric"],
                "result_value": version["result_value"],
                "conclusion": version["conclusion"],
                "tested_by": version["tested_by"],
                "confirmed_by": version["confirmed_by"],
            },
            "items": [dict(row) for row in items],
            "dispositions": [dict(row) for row in dispositions],
            "transfer_flags": [dict(row) for row in flags],
        }

    def lot_trace(self, actor_id: str, lot_id: str) -> dict[str, Any]:
        """从任一库存批次查看来源、影响比例、当前可用量和完整决定链。"""
        self._require(actor_id, "quality.read")
        lot = self.inventory_lot(lot_id)
        held = self._open_holds(lot_id)
        reserved = self._active_reserved(lot_id)
        available = Decimal(lot["available_barrels"])
        usable = available - held - reserved

        sources: list[dict[str, Any]] = []
        visited_lots = {lot_id}
        visited_edges: set[tuple[str, str]] = set()
        frontier: deque[tuple[str, int]] = deque([(lot_id, 0)])
        while frontier:
            current, depth = frontier.popleft()
            blend = self.connection.execute(
                "SELECT * FROM blend_events WHERE target_lot_id=?", (current,)
            ).fetchone()
            if blend is None:
                continue
            lines = self.connection.execute(
                "SELECT bl.*,il.product,il.grade FROM blend_lines bl "
                "JOIN inventory_lots il ON il.lot_id=bl.source_lot_id "
                "WHERE bl.blend_id=? ORDER BY bl.source_lot_id",
                (blend["blend_id"],),
            ).fetchall()
            for line in lines:
                edge = (blend["blend_id"], line["source_lot_id"])
                if edge in visited_edges:
                    continue
                visited_edges.add(edge)
                sources.append({
                    "blend_id": blend["blend_id"],
                    "source_lot_id": line["source_lot_id"],
                    "product": line["product"],
                    "grade": line["grade"],
                    "contributed_barrels": line["quantity_barrels"],
                    "depth": depth + 1,
                })
                if line["source_lot_id"] not in visited_lots:
                    visited_lots.add(line["source_lot_id"])
                    frontier.append((line["source_lot_id"], depth + 1))

        impacts = self.connection.execute(
            "SELECT qi.lot_id,qi.held_barrels,qi.impact_ratio,qi.state,qc.case_id,qc.root_lot_id,qc.status AS case_status "
            "FROM quarantine_items qi JOIN quarantine_cases qc ON qc.case_id=qi.case_id "
            "WHERE qi.lot_id=? ORDER BY qc.opened_at,qc.case_id",
            (lot_id,),
        ).fetchall()
        reservations = self.connection.execute(
            "SELECT reservation_id,quantity_barrels FROM inventory_reservations WHERE lot_id=? AND state='active' "
            "ORDER BY reservation_id",
            (lot_id,),
        ).fetchall()

        entity_keys: set[tuple[str, str]] = {("inventory_lot", lot_id)}
        for row in self.connection.execute("SELECT sample_id FROM quality_samples WHERE lot_id=?", (lot_id,)).fetchall():
            entity_keys.add(("quality_sample", row["sample_id"]))
            for version in self.connection.execute(
                "SELECT version_id FROM quality_test_versions WHERE sample_id=?", (row["sample_id"],)
            ).fetchall():
                entity_keys.add(("quality_test", str(version["version_id"])))
        for row in self.connection.execute(
            "SELECT blend_id FROM blend_events WHERE target_lot_id=? UNION SELECT blend_id FROM blend_lines WHERE source_lot_id=?",
            (lot_id, lot_id),
        ).fetchall():
            entity_keys.add(("blend", row["blend_id"]))
        for row in self.connection.execute(
            "SELECT case_id FROM quarantine_cases WHERE root_lot_id=? UNION SELECT case_id FROM quarantine_items WHERE lot_id=?",
            (lot_id, lot_id),
        ).fetchall():
            entity_keys.add(("quarantine", row["case_id"]))
        for row in self.connection.execute(
            "SELECT transfer_id FROM transfers WHERE inventory_lot_id=?", (lot_id,)
        ).fetchall():
            entity_keys.add(("transfer", row["transfer_id"]))
            entity_keys.add(("transfer_flag", row["transfer_id"]))

        where = " OR ".join("(entity_type=? AND entity_id=?)" for _ in entity_keys)
        params = [value for pair in sorted(entity_keys) for value in pair]
        events = self.connection.execute(
            f"SELECT * FROM supply_audit_events WHERE {where} ORDER BY event_id", params
        ).fetchall()

        return {
            "lot": lot,
            "availability": {
                "quantity_barrels": lot["quantity_barrels"],
                "available_barrels": lot["available_barrels"],
                "held_barrels": decimal_text(quantize_volume(held)),
                "reserved_barrels": decimal_text(quantize_volume(reserved)),
                "usable_barrels": decimal_text(quantize_volume(usable)),
            },
            "sources": sources,
            "impacts": [dict(row) for row in impacts],
            "active_reservations": [dict(row) for row in reservations],
            "decision_chain": [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "actor_id": row["actor_id"],
                    "created_at": row["created_at"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in events
            ],
        }
