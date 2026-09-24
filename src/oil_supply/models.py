"""油气供应领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CRUDE_GRADES = {"BRENT", "WTI", "DUBAI", "ESPO", "URAL", "CUSTOM"}
PRODUCTS = {"crude", "gasoline-92", "gasoline-95", "diesel", "jet-fuel", "condensate"}
ROUTE_KINDS = {"pipeline", "terminal", "refinery", "storage", "truck-rack"}
QUALITY_TEST_CODES = {"RON", "MON", "RON_MON"}
TEST_CONCLUSIONS = {"pass", "fail", "inconclusive"}
DISPOSITION_ACTIONS = {"release", "rework", "downgrade", "destroy"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    price_index: str
    trade_date: str
    close_usd: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        if price_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            price_index=price_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_usd=decimal_value(raw.get("close_usd"), "close_usd", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_barrels=decimal_value(
                raw.get("capacity_barrels"), "capacity_barrels", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("线路起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_barrels: Decimal
    unit_cost_usd: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            unit_cost_usd=decimal_value(
                raw.get("unit_cost_usd"), "unit_cost_usd", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_barrels: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_barrels=decimal_value(
                raw.get("requested_barrels"), "requested_barrels", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    price_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            price_index_drop_percent=decimal_value(
                raw.get("price_index_drop_percent", 0),
                "price_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


@dataclass(frozen=True, slots=True)
class QualitySample:
    sample_id: str
    lot_id: str
    sampled_quantity_barrels: Decimal
    sampled_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualitySample":
        sampled_at = required_text(raw.get("sampled_at"), "sampled_at", 40)
        try:
            parse_utc(sampled_at, "sampled_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            sample_id=identifier(raw.get("sample_id"), "sample_id"),
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            sampled_quantity_barrels=decimal_value(
                raw.get("sampled_quantity_barrels"),
                "sampled_quantity_barrels",
                minimum=Decimal("0.001"),
            ),
            sampled_at=sampled_at,
        )


@dataclass(frozen=True, slots=True)
class QualityTestVersion:
    sample_id: str
    test_code: str
    measured_value: Decimal
    spec_min: Decimal
    spec_max: Decimal
    method: str
    instrument_id: str
    conclusion_override: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualityTestVersion":
        test_code = required_text(raw.get("test_code"), "test_code", 16).upper()
        if test_code not in QUALITY_TEST_CODES:
            raise ValidationFailed("test_code 必须是 RON、MON 或 RON_MON")
        method = required_text(raw.get("method"), "method", 64)
        measured = decimal_value(raw.get("measured_value"), "measured_value")
        spec_min = decimal_value(raw.get("spec_min"), "spec_min")
        spec_max = decimal_value(raw.get("spec_max"), "spec_max")
        if spec_max <= spec_min:
            raise ValidationFailed("spec_max 必须大于 spec_min")
        override = raw.get("conclusion")
        if override is not None:
            override = required_text(override, "conclusion", 16).lower()
            if override not in TEST_CONCLUSIONS:
                raise ValidationFailed("conclusion 必须是 pass、fail 或 inconclusive")
        return cls(
            sample_id=identifier(raw.get("sample_id"), "sample_id"),
            test_code=test_code,
            measured_value=measured,
            spec_min=spec_min,
            spec_max=spec_max,
            method=method,
            instrument_id=identifier(raw.get("instrument_id"), "instrument_id"),
            conclusion_override=override,
        )


@dataclass(frozen=True, slots=True)
class BlendIngredient:
    lot_id: str
    quantity_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BlendIngredient":
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class BlendRecord:
    blend_id: str
    output_lot_id: str
    facility_id: str
    product: str
    grade: str
    output_quantity_barrels: Decimal
    occurred_at: str
    ingredients: tuple[BlendIngredient, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BlendRecord":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        occurred_at = required_text(raw.get("occurred_at"), "occurred_at", 40)
        try:
            parse_utc(occurred_at, "occurred_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        ingredients_raw = raw.get("ingredients")
        if not isinstance(ingredients_raw, list) or not ingredients_raw:
            raise ValidationFailed("混兑必须至少包含一个原料批次")
        if len(ingredients_raw) > 100:
            raise ValidationFailed("一次混兑最多包含 100 个原料批次")
        ingredients = tuple(BlendIngredient.from_dict(item) for item in ingredients_raw)
        lot_ids = [item.lot_id for item in ingredients]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValidationFailed("混兑原料批次不能重复")
        return cls(
            blend_id=identifier(raw.get("blend_id"), "blend_id"),
            output_lot_id=identifier(raw.get("output_lot_id"), "output_lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            output_quantity_barrels=decimal_value(
                raw.get("output_quantity_barrels"),
                "output_quantity_barrels",
                minimum=Decimal("0.001"),
            ),
            occurred_at=occurred_at,
            ingredients=ingredients,
        )


@dataclass(frozen=True, slots=True)
class IsolationCase:
    case_id: str
    sample_id: str
    reason: str
    affected_quantity_barrels: Decimal | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IsolationCase":
        affected_raw = raw.get("affected_quantity_barrels")
        affected = (
            None
            if affected_raw is None
            else decimal_value(
                affected_raw, "affected_quantity_barrels", minimum=Decimal("0.001")
            )
        )
        return cls(
            case_id=identifier(raw.get("case_id"), "case_id"),
            sample_id=identifier(raw.get("sample_id"), "sample_id"),
            reason=required_text(raw.get("reason"), "reason"),
            affected_quantity_barrels=affected,
        )


@dataclass(frozen=True, slots=True)
class DispositionInstruction:
    target_id: int
    action: str
    quantity_barrels: Decimal
    note: str
    destination_lot_id: str | None
    product: str | None
    grade: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DispositionInstruction":
        action = required_text(raw.get("action"), "action", 16).lower()
        if action not in DISPOSITION_ACTIONS:
            raise ValidationFailed("action 必须是 release、rework、downgrade 或 destroy")
        target = raw.get("target_id")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValidationFailed("target_id 必须是正整数")
        destination = raw.get("destination_lot_id")
        product = raw.get("product")
        grade = raw.get("grade")
        if action in {"rework", "downgrade"}:
            destination = identifier(destination, "destination_lot_id")
            product = required_text(product, "product", 32)
            if product not in PRODUCTS:
                raise ValidationFailed("product 不是受支持的油品")
            grade = required_text(grade, "grade", 32).upper()
        elif destination is not None or product is not None or grade is not None:
            raise ValidationFailed("只有 rework 或 downgrade 才能指定目标批次")
        note_value = raw.get("note")
        return cls(
            target_id=target,
            action=action,
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            note=required_text(note_value, "note", 512) if note_value else "",
            destination_lot_id=destination,
            product=product,
            grade=grade,
        )


@dataclass(frozen=True, slots=True)
class InventoryReservation:
    reservation_id: str
    lot_id: str
    quantity_barrels: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryReservation":
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
