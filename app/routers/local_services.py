from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_pool
from app.repositories.shared import execute_fetch_all_dict, fetch_all_dict


router = APIRouter(prefix="/local", tags=["local-services"])


class PushTokenRequest(BaseModel):
    expoPushToken: str = Field(min_length=1, max_length=512)
    devicePlatform: str = Field(min_length=1, max_length=32)


class DerivationContact(BaseModel):
    deliveryType: str = Field(pattern="^(to|cc)$")
    email: str = Field(min_length=3, max_length=320)
    isActive: bool = True
    notifyOnCompletion: bool = False
    recipientKey: str = Field(min_length=1, max_length=80)
    recipientLabel: str = Field(min_length=1, max_length=120)
    sortOrder: int = Field(default=0, ge=0, le=9999)
    visibleInMobile: bool = True


class SupplyCodesRequest(BaseModel):
    supplyCodes: list[str] = Field(default_factory=list, max_length=2000)


def require_authenticated(auth_user_id: str | None) -> str:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    return auth_user_id


@router.get("/profile")
async def local_profile(auth_user_id: str | None = Header(default=None, alias="x-auth-user-id")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    rows = await fetch_all_dict(
        get_pool(),
        """
        SELECT p.id::text AS id, p.full_name, p.role::text AS role,
          p.employee_code, p.active, p.avatar_url,
          a.email, a.username, a.assignment_code, a.legacy_user_id,
          a.last_login_at::text AS last_login_at
        FROM public.profiles p
        LEFT JOIN public.auth_profiles_local a ON a.auth_user_id = p.id
        WHERE p.id = %s::uuid
        LIMIT 1
        """,
        [auth_user_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Perfil local no encontrado.")
    return rows[0]


@router.post("/profile/touch")
async def touch_local_profile(auth_user_id: str | None = Header(default=None, alias="x-auth-user-id")) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    await execute_fetch_all_dict(
        get_pool(),
        """
        UPDATE public.auth_profiles_local SET last_login_at = now(), updated_at = now()
        WHERE auth_user_id = %s::uuid RETURNING auth_user_id
        """,
        [auth_user_id],
    )
    return {"success": True}


@router.post("/push-tokens")
async def register_push_token(
    payload: PushTokenRequest = Body(...),
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="No autenticado.")
    rows = await execute_fetch_all_dict(
        get_pool(),
        """
        INSERT INTO public.push_tokens (profile_id, expo_push_token, device_platform, updated_at)
        VALUES (%s::uuid, %s, %s, now())
        ON CONFLICT (profile_id, expo_push_token) DO UPDATE SET
          device_platform = EXCLUDED.device_platform, updated_at = now()
        RETURNING id
        """,
        [auth_user_id, payload.expoPushToken, payload.devicePlatform],
    )
    return {"success": bool(rows)}


@router.get("/anomalies/map")
async def anomaly_map_items() -> list[dict]:
    return await fetch_all_dict(
        get_pool(),
        """
        SELECT a.id, a.supply_code, a.anomaly_type, a.anomaly_status,
          a.customer_name_snapshot, a.average_consumption,
          cs.district, cs.latitude, cs.longitude,
          COALESCE(cs.office_name, cs.service_address) AS office_name
        FROM public.anomalies a
        JOIN public.customer_supplies cs ON cs.supply_code = a.supply_code
        WHERE cs.latitude IS NOT NULL AND cs.longitude IS NOT NULL
        ORDER BY a.created_at DESC NULLS LAST
        LIMIT 500
        """,
    )


@router.get("/derivation-recipients")
async def derivation_recipients(
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    # require_authenticated(auth_user_id)
    rows = await fetch_all_dict(
        get_pool(),
        """
        SELECT id, recipient_key, recipient_label, email, delivery_type,
          is_active, sort_order, notify_on_completion, visible_in_mobile
        FROM public.derivation_recipient_contacts
        ORDER BY sort_order, recipient_label, id
        """,
    )
    return {"contacts": [{
        "id": row["id"], "recipientKey": row["recipient_key"],
        "recipientLabel": row["recipient_label"], "email": row["email"],
        "deliveryType": row["delivery_type"], "isActive": row["is_active"],
        "sortOrder": row["sort_order"], "notifyOnCompletion": row["notify_on_completion"],
        "visibleInMobile": row["visible_in_mobile"],
    } for row in rows]}


@router.put("/derivation-recipients")
async def replace_derivation_recipients(
    contacts: list[DerivationContact] = Body(...),
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    require_authenticated(auth_user_id)
    if (user_role or "").lower() not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Se requiere rol administrador.")
    async with get_pool().connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("DELETE FROM public.derivation_recipient_contacts")
            for contact in contacts:
                await cursor.execute(
                    """
                    INSERT INTO public.derivation_recipient_contacts (
                      recipient_key, recipient_label, email, delivery_type, is_active,
                      sort_order, notify_on_completion, visible_in_mobile
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [contact.recipientKey, contact.recipientLabel, contact.email.lower(),
                     contact.deliveryType, contact.isActive, contact.sortOrder,
                     contact.notifyOnCompletion, contact.visibleInMobile],
                )
        await connection.commit()
    return await derivation_recipients(auth_user_id)


@router.post("/supplies/batch")
async def local_supplies_batch(
    payload: SupplyCodesRequest = Body(...),
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    require_authenticated(auth_user_id)
    codes = sorted({str(value).strip() for value in payload.supplyCodes if str(value).strip()})
    if not codes:
        return {"data": []}
    rows = await fetch_all_dict(get_pool(), """
      SELECT cs.id, cs.customer_id, cs.supply_code, cs.customer_name, cs.segment,
        cs.supply_status, cs.service_address, cs.district, cs.geolocation_address,
        cs.latitude, cs.longitude, cs.location_source, cs.meter_code, cs.office_name,
        c.business_name, c.full_name, c.first_name, c.last_name, c.id_doc_number,
        c.payer_classification, c.phone_primary, c.phone_secondary
      FROM public.customer_supplies cs
      LEFT JOIN public.customers c ON c.id = cs.customer_id
      WHERE cs.supply_code = ANY(%s)
      ORDER BY cs.supply_code LIMIT 2000
    """, [codes])
    return {"data": rows}


@router.get("/users/assignable")
async def local_assignable_users(
    name: str | None = Query(default=None, max_length=160),
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    require_authenticated(auth_user_id)
    params: list = []
    clause = ""
    if name and name.strip():
        clause = "WHERE lower(trim(u.full_name)) = lower(trim(%s))"
        params.append(name.strip())
    rows = await fetch_all_dict(get_pool(), f"""
      SELECT u.id, u.full_name, u.email FROM public.users u
      {clause} ORDER BY u.full_name, u.id LIMIT 500
    """, params)
    return {"data": rows}


@router.get("/customers/summary")
async def local_customers_summary(
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    require_authenticated(auth_user_id)
    rows = await fetch_all_dict(get_pool(), """
      SELECT count(DISTINCT customer_id)::int AS total_customers,
        count(*)::int AS total_supplies
      FROM public.customer_supplies
    """)
    row = rows[0] if rows else {"total_customers": 0, "total_supplies": 0}
    return {"totalCustomers": row["total_customers"], "totalSupplies": row["total_supplies"], "totalDebt": 0}


@router.get("/customers/{customer_id}/supplies")
async def local_customer_supplies(
    customer_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    require_authenticated(auth_user_id)
    count = await fetch_all_dict(get_pool(), "SELECT count(*)::int AS total FROM public.customer_supplies WHERE customer_id = %s", [customer_id])
    rows = await fetch_all_dict(get_pool(), """
      SELECT id, supply_code, geolocation_address, service_address, district, latitude,
        longitude, location_source, meter_code, supply_status, office_name
      FROM public.customer_supplies WHERE customer_id = %s
      ORDER BY supply_code LIMIT %s OFFSET %s
    """, [customer_id, page_size, (page - 1) * page_size])
    return {"data": rows, "total": count[0]["total"] if count else 0}


@router.get("/inspections/filters")
async def local_inspection_filters(
    auth_user_id: str | None = Header(default=None, alias="x-auth-user-id"),
) -> dict:
    require_authenticated(auth_user_id)
    districts = await fetch_all_dict(get_pool(), """
      SELECT DISTINCT district FROM public.commercial_inspections
      WHERE district IS NOT NULL AND trim(district) <> '' ORDER BY district LIMIT 500
    """)
    typologies = await fetch_all_dict(get_pool(), """
      SELECT DISTINCT inspection_typology FROM public.commercial_inspections
      WHERE inspection_typology IS NOT NULL AND trim(inspection_typology) <> ''
      ORDER BY inspection_typology LIMIT 500
    """)
    return {"districts": [row["district"] for row in districts],
            "typologies": [row["inspection_typology"] for row in typologies]}
