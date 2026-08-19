from fastapi import APIRouter, Body, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.authz import ensure_path_access, normalize_assignment_code, normalize_role
from app.database import get_pool
from app.jwt_auth import (
    create_access_token,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_access_token,
)
from app.passwords import hash_password
from app.passwords import verify_password
from app.repositories.auth import (
    create_user,
    create_view,
    find_user_by_identifier,
    find_user_for_login,
    get_access_snapshot,
    get_user_by_id,
    get_user_credentials_by_id,
    get_user_views,
    get_view_by_id,
    list_users,
    list_views,
    replace_user_views,
    touch_user_last_login,
    update_user,
    update_user_password,
    update_view,
)
from app.repositories.shared import execute_fetch_all_dict, execute_statement, fetch_all_dict
from app.supabase_auth import get_supabase_context, read_bearer_token, to_session_user


router = APIRouter(prefix="/auth", tags=["auth"])
PERSONAL_PATH = "/personal"


class UserCreateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=160)
    username: str = Field(min_length=3, max_length=80)
    fullName: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=6, max_length=160)
    assignmentCode: str | None = Field(default=None, max_length=80)
    isActive: bool = True
    role: str = Field(default="supervisor", min_length=1, max_length=40)


class UserUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=160)
    username: str = Field(min_length=3, max_length=80)
    fullName: str = Field(min_length=3, max_length=160)
    assignmentCode: str | None = Field(default=None, max_length=80)
    isActive: bool
    role: str = Field(min_length=1, max_length=40)


class UserPasswordUpdateRequest(BaseModel):
    password: str = Field(min_length=6, max_length=160)


class UserIdentityRequest(BaseModel):
    authUserId: str = Field(min_length=36, max_length=36)
    email: str = Field(min_length=3, max_length=160)
    username: str = Field(min_length=1, max_length=80)


class ViewPayload(BaseModel):
    code: str = Field(min_length=2, max_length=80)
    label: str = Field(min_length=2, max_length=120)
    path: str = Field(min_length=2, max_length=180)
    apiPrefix: str | None = Field(default=None, max_length=180)
    groupKey: str = Field(min_length=2, max_length=80)
    groupLabel: str = Field(min_length=2, max_length=120)
    icon: str | None = Field(default=None, max_length=80)
    sortOrder: int = Field(default=0, ge=0, le=9999)
    isActive: bool = True
    isHomeEligible: bool = False


class UserViewPermissionsPayload(BaseModel):
    viewIds: list[int] = Field(default_factory=list)


class RolePayload(BaseModel):
    code: str | None = Field(default=None, min_length=2, max_length=80)
    label: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=500)
    permissionIds: list[str] | None = None


class PermissionPayload(BaseModel):
    code: str = Field(min_length=2, max_length=120)
    resource: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=500)
    viewId: int | None = Field(default=None, ge=1)


class MobileLoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=160)


class NativeLoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=160)


class NativeRefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=10)


class NativeLogoutRequest(BaseModel):
    refreshToken: str = Field(min_length=10)


class SelfPasswordUpdateRequest(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=160)
    newPassword: str = Field(min_length=6, max_length=160)


def normalize_identity(value: str) -> str:
    return value.strip().lower()


def normalize_text(value: str) -> str:
    return value.strip()


async def ensure_personal_module_access(
    user_id: int | None,
    user_role: str | None,
) -> dict:
    return await ensure_path_access(
        get_pool(),
        user_id=user_id,
        user_role=user_role,
        path=PERSONAL_PATH,
    )


@router.get("/user")
async def get_auth_user(
    identifier: str = Query(..., min_length=1, max_length=120),
) -> dict | None:
    return await find_user_by_identifier(get_pool(), normalize_identity(identifier))


@router.post("/mobile/login")
async def post_mobile_login(payload: MobileLoginRequest = Body(...)) -> dict:
    raise HTTPException(
        status_code=410,
        detail="La autenticacion movil ahora usa directamente Supabase Auth. Este endpoint ya no debe usarse.",
    )


@router.post("/login")
async def post_login(
    payload: NativeLoginRequest = Body(...),
    user_agent: str | None = Header(default=None, alias="User-Agent"),
) -> dict:
    """Auth propio (Fase 2): JWT access de corta duracion + refresh token
    opaco de larga duracion. Reemplaza a Supabase Auth para clientes ya
    migrados; convive con Supabase mientras dure la Fase 2 (modo dual)."""
    identifier = normalize_identity(payload.identifier)
    user = await find_user_for_login(get_pool(), identifier)
    if not user or not user.get("is_active") or not verify_password(payload.password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="Usuario o contrasena incorrectos.")

    await touch_user_last_login(get_pool(), user["id"])
    access_token, access_expires_at = create_access_token(user_id=user["id"], role=user.get("role"))
    refresh_token = await issue_refresh_token(get_pool(), user_id=user["id"], user_agent=user_agent)
    access = await get_access_snapshot(get_pool(), user_id=user["id"], role=user.get("role"))

    user.pop("password_hash", None)
    return {
        "accessToken": access_token,
        "accessTokenExpiresAt": access_expires_at,
        "refreshToken": refresh_token,
        "user": user,
        "access": access,
    }


@router.post("/refresh")
async def post_refresh(
    payload: NativeRefreshRequest = Body(...),
    user_agent: str | None = Header(default=None, alias="User-Agent"),
) -> dict:
    result = await rotate_refresh_token(get_pool(), payload.refreshToken, user_agent=user_agent)
    user = result["user"]
    access = await get_access_snapshot(get_pool(), user_id=user["id"], role=user.get("role"))
    user = {**user}
    user.pop("password_hash", None)
    return {
        "accessToken": result["accessToken"],
        "accessTokenExpiresAt": result["accessTokenExpiresAt"],
        "refreshToken": result["refreshToken"],
        "user": user,
        "access": access,
    }


@router.post("/logout")
async def post_logout(payload: NativeLogoutRequest = Body(...)) -> dict:
    await revoke_refresh_token(get_pool(), payload.refreshToken)
    return {"success": True}


@router.get("/session")
async def get_session(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """Valida el JWT propio (Authorization: Bearer) y devuelve el usuario +
    su snapshot de acceso. Equivalente nativo de GET /auth/mobile/me (que
    valida JWT de Supabase)."""
    token = read_bearer_token(authorization)
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="No autenticado.")

    user = await get_user_by_id(get_pool(), int(payload["sub"]))
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="Usuario inactivo o no encontrado.")

    access = await get_access_snapshot(get_pool(), user_id=user["id"], role=user.get("role"))
    return {"user": user, "access": access}


@router.patch("/me/password")
async def patch_auth_me_password(
    payload: SelfPasswordUpdateRequest = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """Cambio de password self-service para un usuario nativo (Fase 2), sin
    depender de acceso al modulo /personal (a diferencia de
    PATCH /auth/users/{id}/password, pensado para que un admin gestione a
    otros). Requiere el JWT propio (Authorization: Bearer) y la contrasena
    actual correcta."""
    token = read_bearer_token(authorization)
    claims = verify_access_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="No autenticado.")

    user_id = int(claims["sub"])
    credentials = await get_user_credentials_by_id(get_pool(), user_id)
    if not credentials or not credentials.get("is_active"):
        raise HTTPException(status_code=401, detail="Usuario inactivo o no encontrado.")

    if not verify_password(payload.currentPassword, credentials.get("password_hash")):
        raise HTTPException(status_code=401, detail="La contrasena actual es incorrecta.")

    await update_user_password(get_pool(), user_id, password_hash=hash_password(payload.newPassword))
    return {"success": True}


@router.get("/mobile/me")
async def get_mobile_me(authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
    context = await get_supabase_context(get_pool(), authorization)
    if not context:
        raise HTTPException(status_code=401, detail="No autenticado.")
    return {
        "access": context["access"],
        "user": to_session_user(context["user"]),
    }


@router.post("/user/{user_id}/touch-last-login")
async def post_touch_last_login(
    user_id: int = Path(..., ge=1),
) -> dict[str, bool]:
    await touch_user_last_login(get_pool(), user_id)
    return {"success": True}


@router.get("/me/access")
async def get_auth_me_access(
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    if user_id is None:
        raise HTTPException(status_code=401, detail="No autenticado.")
    return await get_access_snapshot(
        get_pool(),
        user_id=user_id,
        role=user_role,
    )


@router.get("/users")
async def get_auth_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=120),
    filter_view: str | None = Query(default=None, max_length=180),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    return await list_users(
        get_pool(),
        page=page,
        page_size=page_size,
        search=search,
        filter_view=filter_view,
    )


def resolve_user_email(username: str, email: str | None) -> str:
    cleaned = (email or "").strip().lower()
    return cleaned if cleaned else f"{normalize_identity(username)}@sedapal.com.pe"


@router.post("/users")
async def post_auth_user(
    payload: UserCreateRequest = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(user_id, user_role)
    email = resolve_user_email(payload.username, payload.email)
    return await create_user(
        get_pool(),
        email=email,
        username=normalize_identity(payload.username),
        full_name=normalize_text(payload.fullName),
        password_hash=hash_password(payload.password),
        role=normalize_role(payload.role),
        assignment_code=normalize_assignment_code(payload.assignmentCode),
        is_active=payload.isActive,
    )


@router.get("/users/{user_id}")
async def get_auth_user_detail(
    user_id: int = Path(..., ge=1),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(request_user_id, request_user_role)
    return await get_user_by_id(get_pool(), user_id)


@router.patch("/users/{user_id}")
async def patch_auth_user(
    user_id: int = Path(..., ge=1),
    payload: UserUpdateRequest = Body(...),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(request_user_id, request_user_role)
    email = resolve_user_email(payload.username, payload.email)
    return await update_user(
        get_pool(),
        user_id,
        assignment_code=normalize_assignment_code(payload.assignmentCode),
        is_active=payload.isActive,
        role=normalize_role(payload.role),
        email=email,
        full_name=normalize_text(payload.fullName),
        username=normalize_identity(payload.username),
    )


@router.patch("/users/{user_id}/password")
async def patch_auth_user_password(
    user_id: int = Path(..., ge=1),
    payload: UserPasswordUpdateRequest = Body(...),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(request_user_id, request_user_role)
    return await update_user_password(
        get_pool(),
        user_id,
        password_hash=hash_password(payload.password),
    )


@router.put("/users/{user_id}/identity")
async def put_auth_user_identity(
    user_id: int = Path(..., ge=1),
    payload: UserIdentityRequest = Body(...),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(request_user_id, request_user_role)
    user = await get_user_by_id(get_pool(), user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario local no encontrado.")
    profile_role = "superadmin" if normalize_role(user.get("role")) == "admin" else "supervisor"
    await execute_statement(
        get_pool(),
        """
        INSERT INTO public.profiles (id, full_name, role, employee_code, active, updated_at)
        VALUES (%s::uuid, %s, %s::user_role, %s, %s, now())
        ON CONFLICT (id) DO UPDATE SET full_name = EXCLUDED.full_name,
          role = EXCLUDED.role, employee_code = EXCLUDED.employee_code,
          active = EXCLUDED.active, updated_at = now()
        """,
        [payload.authUserId, user["full_name"], profile_role, user.get("assignment_code"), user["is_active"]],
    )
    await execute_statement(
        get_pool(),
        """
        DELETE FROM public.auth_profiles_local
        WHERE legacy_user_id = %s
           OR lower(username) = %s
           OR lower(email) = %s
           OR auth_user_id = %s::uuid;
        """,
        [user_id, payload.username.lower(), payload.email.lower(), payload.authUserId],
    )
    await execute_statement(
        get_pool(),
        """
        INSERT INTO public.auth_profiles_local (
          auth_user_id, email, username, assignment_code, legacy_user_id, updated_at
        ) VALUES (%s::uuid, %s, %s, %s, %s, now())
        ON CONFLICT (auth_user_id) DO UPDATE SET email = EXCLUDED.email,
          username = EXCLUDED.username, assignment_code = EXCLUDED.assignment_code,
          legacy_user_id = EXCLUDED.legacy_user_id, updated_at = now()
        """,
        [payload.authUserId, payload.email.lower(), payload.username.lower(), user.get("assignment_code"), user_id],
    )
    return {"success": True, "authUserId": payload.authUserId}


@router.get("/users/{user_id}/views")
async def get_auth_user_views(
    user_id: int = Path(..., ge=1),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(request_user_id, request_user_role)
    return await get_user_views(get_pool(), user_id)


@router.put("/users/{user_id}/views")
async def put_auth_user_views(
    user_id: int = Path(..., ge=1),
    payload: UserViewPermissionsPayload = Body(...),
    request_user_id: int | None = Header(default=None, alias="x-user-id"),
    request_user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(request_user_id, request_user_role)
    return await replace_user_views(
        get_pool(),
        user_id,
        view_ids=payload.viewIds,
        granted_by_user_id=request_user_id,
    )


@router.get("/views")
async def get_auth_views(
    include_inactive: bool = Query(default=True),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    return {"data": await list_views(get_pool(), include_inactive=include_inactive)}


@router.post("/views")
async def post_auth_view(
    payload: ViewPayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(user_id, user_role)
    return await create_view(
        get_pool(),
        code=normalize_identity(payload.code),
        label=normalize_text(payload.label),
        path=normalize_text(payload.path),
        api_prefix=normalize_text(payload.apiPrefix) if payload.apiPrefix else None,
        group_key=normalize_identity(payload.groupKey),
        group_label=normalize_text(payload.groupLabel),
        icon=normalize_text(payload.icon) if payload.icon else None,
        sort_order=payload.sortOrder,
        is_active=payload.isActive,
        is_home_eligible=payload.isHomeEligible,
    )


@router.get("/views/{view_id}")
async def get_auth_view_detail(
    view_id: int = Path(..., ge=1),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(user_id, user_role)
    return await get_view_by_id(get_pool(), view_id)


@router.patch("/views/{view_id}")
async def patch_auth_view(
    view_id: int = Path(..., ge=1),
    payload: ViewPayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(user_id, user_role)
    return await update_view(
        get_pool(),
        view_id,
        code=normalize_identity(payload.code),
        label=normalize_text(payload.label),
        path=normalize_text(payload.path),
        api_prefix=normalize_text(payload.apiPrefix) if payload.apiPrefix else None,
        group_key=normalize_identity(payload.groupKey),
        group_label=normalize_text(payload.groupLabel),
        icon=normalize_text(payload.icon) if payload.icon else None,
        sort_order=payload.sortOrder,
        is_active=payload.isActive,
        is_home_eligible=payload.isHomeEligible,
    )


def _role_response(row: dict, permission_ids: list[str] | None = None) -> dict:
    result = {
        "id": str(row["id"]), "code": row["code"], "label": row["label"],
        "description": row.get("description") or "", "isSystem": bool(row.get("is_system")),
    }
    if permission_ids is not None:
        result["assignedPermissionIds"] = permission_ids
    if "users_count" in row:
        result["usersCount"] = int(row.get("users_count") or 0)
        result["permissionsCount"] = int(row.get("permissions_count") or 0)
    return result


@router.get("/roles")
async def get_auth_roles(
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await fetch_all_dict(get_pool(), """
      SELECT r.id::text, r.code, r.label, r.description, r.is_system,
        count(DISTINCT u.id)::int AS users_count,
        count(DISTINCT rp.permission_id)::int AS permissions_count
      FROM public.roles r
      LEFT JOIN public.users u ON u.role = r.code
      LEFT JOIN public.role_permissions rp ON rp.role_id = r.id
      GROUP BY r.id ORDER BY r.label
    """)
    return {"data": [_role_response(row) for row in rows]}


@router.post("/roles")
async def post_auth_role(
    payload: RolePayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    if not payload.code:
        raise HTTPException(status_code=422, detail="El codigo del rol es obligatorio.")
    rows = await execute_fetch_all_dict(get_pool(), """
      INSERT INTO public.roles (code, label, description, is_system)
      VALUES (%s, %s, %s, false)
      RETURNING id::text, code, label, description, is_system
    """, [normalize_identity(payload.code), normalize_text(payload.label), payload.description.strip()])
    row = rows[0]
    for permission_id in sorted(set(payload.permissionIds or [])):
        await execute_fetch_all_dict(get_pool(), """
          INSERT INTO public.role_permissions (role_id, permission_id)
          VALUES (%s::uuid, %s::uuid) ON CONFLICT DO NOTHING RETURNING role_id
        """, [row["id"], permission_id])
    result = _role_response(row)
    result.update({"permissionsCount": len(set(payload.permissionIds or [])), "usersCount": 0})
    return result


@router.get("/roles/{role_id}")
async def get_auth_role(
    role_id: str,
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict | None:
    await ensure_personal_module_access(user_id, user_role)
    rows = await fetch_all_dict(get_pool(), """
      SELECT id::text, code, label, description, is_system FROM public.roles WHERE id = %s::uuid LIMIT 1
    """, [role_id])
    if not rows:
        return None
    links = await fetch_all_dict(get_pool(), "SELECT permission_id::text FROM public.role_permissions WHERE role_id = %s::uuid", [role_id])
    return _role_response(rows[0], [row["permission_id"] for row in links])


@router.patch("/roles/{role_id}")
async def patch_auth_role(
    role_id: str,
    payload: RolePayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), """
      UPDATE public.roles SET label = %s, description = %s, updated_at = now()
      WHERE id = %s::uuid RETURNING id::text, code, label, description, is_system
    """, [normalize_text(payload.label), payload.description.strip(), role_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Rol no encontrado.")
    if payload.permissionIds is not None:
        await execute_fetch_all_dict(get_pool(), "DELETE FROM public.role_permissions WHERE role_id = %s::uuid RETURNING role_id", [role_id])
        for permission_id in sorted(set(payload.permissionIds)):
            await execute_fetch_all_dict(get_pool(), """
              INSERT INTO public.role_permissions (role_id, permission_id)
              VALUES (%s::uuid, %s::uuid) ON CONFLICT DO NOTHING RETURNING role_id
            """, [role_id, permission_id])
    return _role_response(rows[0], payload.permissionIds or [])


@router.delete("/roles/{role_id}")
async def delete_auth_role(
    role_id: str,
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await fetch_all_dict(get_pool(), "SELECT is_system FROM public.roles WHERE id = %s::uuid", [role_id])
    if not rows:
        return {"deleted": False}
    if rows[0]["is_system"]:
        raise HTTPException(status_code=409, detail="No se puede eliminar un rol del sistema.")
    deleted = await execute_fetch_all_dict(get_pool(), "DELETE FROM public.roles WHERE id = %s::uuid RETURNING id", [role_id])
    return {"deleted": bool(deleted)}


def _permission_response(row: dict) -> dict:
    return {
        "id": str(row["id"]), "code": row["code"], "resource": row["resource"],
        "action": row["action"], "label": row["label"], "description": row.get("description") or "",
        "viewId": str(row["view_id"]) if row.get("view_id") else None,
        "viewLabel": row.get("view_label"),
    }


@router.get("/permissions")
async def get_auth_permissions(
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await fetch_all_dict(get_pool(), """
      SELECT p.id::text, p.code, p.resource, p.action, p.label, p.description,
        p.view_id, v.label AS view_label
      FROM public.permissions p LEFT JOIN public.app_views v ON v.id = p.view_id
      ORDER BY p.resource, p.action
    """)
    return {"data": [_permission_response(row) for row in rows]}


@router.post("/permissions")
async def post_auth_permission(
    payload: PermissionPayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), """
      INSERT INTO public.permissions (code, resource, action, label, description, view_id)
      VALUES (%s, %s, %s, %s, %s, %s)
      RETURNING id::text, code, resource, action, label, description, view_id
    """, [normalize_identity(payload.code), payload.resource.strip(), payload.action.strip(),
          normalize_text(payload.label), payload.description.strip(), payload.viewId])
    return _permission_response(rows[0])


@router.patch("/permissions/{permission_id}")
async def patch_auth_permission(
    permission_id: str,
    payload: PermissionPayload = Body(...),
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), """
      UPDATE public.permissions SET code = %s, resource = %s, action = %s, label = %s,
        description = %s, view_id = %s, updated_at = now()
      WHERE id = %s::uuid
      RETURNING id::text, code, resource, action, label, description, view_id
    """, [normalize_identity(payload.code), payload.resource.strip(), payload.action.strip(),
          normalize_text(payload.label), payload.description.strip(), payload.viewId, permission_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Permiso no encontrado.")
    return _permission_response(rows[0])


@router.delete("/permissions/{permission_id}")
async def delete_auth_permission(
    permission_id: str,
    user_id: int | None = Header(default=None, alias="x-user-id"),
    user_role: str | None = Header(default=None, alias="x-user-role"),
) -> dict:
    await ensure_personal_module_access(user_id, user_role)
    rows = await execute_fetch_all_dict(get_pool(), "DELETE FROM public.permissions WHERE id = %s::uuid RETURNING id", [permission_id])
    return {"deleted": bool(rows)}
