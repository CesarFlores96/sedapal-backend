from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.database import get_pool
from app.repositories.shared import fetch_all_dict


router = APIRouter(prefix="/analytics", tags=["analytics"])


class AnalyticsMetric(BaseModel):
    aggregation: Literal["sum", "avg", "count", "min", "max"]
    alias: str = Field(pattern=r"^[a-z_][a-z0-9_]*$", max_length=40)
    field: Literal["billed_volume_m3", "amount_soles", "records"]


class AnalyticsFilter(BaseModel):
    field: Literal["supply_code", "customer_name", "district", "office_code", "year", "month"]
    operator: Literal["eq", "in", "between", "gte", "lte", "contains"]
    value: str | int | list[str | int]


class BillingAnalyticsRequest(BaseModel):
    dataset: Literal["water_billing_history"]
    dimensions: list[Literal["supply_code", "customer_name", "district", "office_code", "year", "month"]] = Field(default_factory=list, max_length=3)
    filters: list[AnalyticsFilter] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=100, ge=1, le=200)
    metrics: list[AnalyticsMetric] = Field(min_length=1, max_length=5)
    order_by: list[tuple[str, Literal["asc", "desc"]]] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_request(self) -> "BillingAnalyticsRequest":
        aliases = {metric.alias for metric in self.metrics}
        if len(aliases) != len(self.metrics):
            raise ValueError("Los alias de metricas deben ser unicos.")
        allowed_order = aliases | set(self.dimensions)
        if any(field not in allowed_order for field, _direction in self.order_by):
            raise ValueError("El orden solo puede usar dimensiones o metricas solicitadas.")
        return self


DIMENSIONS = {
    "supply_code": "billing.supply_code",
    "customer_name": "COALESCE(supply.customer_name, 'SUMINISTRO ' || billing.supply_code)",
    "district": "supply.district",
    "office_code": "billing.office_code",
    "year": "EXTRACT(YEAR FROM billing.issue_date)::int",
    "month": "EXTRACT(MONTH FROM billing.issue_date)::int",
}

METRICS = {
    "billed_volume_m3": "billing.billed_volume_m3",
    "amount_soles": "billing.amount_soles",
    "records": "*",
}

AGGREGATIONS = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "min": "MIN",
    "max": "MAX",
}

def _add_filter(where: list[str], values: list[object], expression: str, item: AnalyticsFilter) -> None:
    if item.operator == "eq":
        values.append(item.value)
        where.append(f"{expression} = %s")
    elif item.operator == "gte":
        values.append(item.value)
        where.append(f"{expression} >= %s")
    elif item.operator == "lte":
        values.append(item.value)
        where.append(f"{expression} <= %s")
    elif item.operator == "in":
        values.append(item.value if isinstance(item.value, list) else [item.value])
        where.append(f"{expression} = ANY(%s)")
    elif item.operator == "contains":
        if not isinstance(item.value, str) or not item.value.strip():
            raise ValueError("El filtro contains requiere un texto.")
        escaped = item.value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.append(f"%{escaped}%")
        where.append(f"{expression} ILIKE %s ESCAPE '\\'")
    else:
        if not isinstance(item.value, list) or len(item.value) != 2:
            raise ValueError("El filtro between requiere dos valores.")
        values.extend(item.value)
        where.append(f"{expression} BETWEEN %s AND %s")


@router.post("/billing")
async def billing_analytics(payload: BillingAnalyticsRequest) -> dict:
    """Ejecuta el dataset de facturacion permitido; nunca recibe SQL libre."""
    dimensions = list(dict.fromkeys(payload.dimensions))
    select_parts = [f"{DIMENSIONS[item]} AS \"{item}\"" for item in dimensions]

    for metric in payload.metrics:
        expression = METRICS[metric.field]
        if metric.field == "records" and metric.aggregation != "count":
            raise ValueError("La metrica records solo permite count.")
        select_parts.append(f"{AGGREGATIONS[metric.aggregation]}({expression}) AS \"{metric.alias}\"")

    # SEDAPAL is the operating company, not a comparable customer. It must not
    # inflate or appear in consumption rankings requested by users.
    where = [
        "billing.issue_date IS NOT NULL",
        "billing.concept = 'consumo_agua'",
        "COALESCE(supply.customer_name, '') NOT ILIKE %s",
    ]
    values: list[object] = ["%SEDAPAL%"]
    for item in payload.filters:
        _add_filter(where, values, DIMENSIONS[item.field], item)

    group_by = f"GROUP BY {', '.join(DIMENSIONS[item] for item in dimensions)}" if dimensions else ""
    requested_order = [
        f'"{field}" {direction.upper()}'
        for field, direction in payload.order_by
    ]
    order_by = f"ORDER BY {', '.join(requested_order)}" if requested_order else ""
    query = f"""
        SELECT {', '.join(select_parts)}
        FROM public.customer_supply_billing_daily billing
        LEFT JOIN public.customer_supplies supply ON supply.supply_code = billing.supply_code
        WHERE {' AND '.join(where)}
        {group_by}
        {order_by}
        LIMIT {payload.limit}
    """
    rows = await fetch_all_dict(get_pool(), query, values)
    return {"data": rows, "total_returned": len(rows)}
