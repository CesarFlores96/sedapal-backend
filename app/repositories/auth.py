from collections import OrderedDict

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.repositories.shared import execute_statement, fetch_all_dict

_users_schema_ready = False

LEGACY_FALLBACK_VIEWS: dict[str, list[dict]] = {
    "admin": [
        {
            "api_prefix": "/api/dashboard",
            "code": "dashboard",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "LayoutDashboard",
            "is_active": True,
            "is_home_eligible": True,
            "label": "Inicio",
            "path": "/dashboard",
            "sort_order": 10,
        },
        {
            "api_prefix": "/api/gis/",
            "code": "mapa",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Map",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Mapa GIS",
            "path": "/mapa",
            "sort_order": 20,
        },
        {
            "api_prefix": "/api/customers",
            "code": "clientes",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "UsersRound",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Catastro",
            "path": "/clientes",
            "sort_order": 30,
        },
        {
            "api_prefix": "/api/reportes/",
            "code": "reportes",
            "group_key": "gestion",
            "group_label": "GESTION",
            "icon": "ClipboardList",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Reportes",
            "path": "/reportes",
            "sort_order": 40,
        },
        {
            "api_prefix": "/api/supervisiones",
            "code": "supervisiones",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "ClipboardPen",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Supervisiones",
            "path": "/supervisiones",
            "sort_order": 50,
        },
        {
            "api_prefix": "/api/auth/",
            "code": "personal",
            "group_key": "gestion",
            "group_label": "GESTION",
            "icon": "ShieldCheck",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Personal",
            "path": "/personal",
            "sort_order": 60,
        },
        {
            "api_prefix": "/api/data-sources",
            "code": "fuentes-datos",
            "group_key": "gestion",
            "group_label": "GESTION",
            "icon": "Database",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Fuentes de datos",
            "path": "/fuentes-datos",
            "sort_order": 70,
        },
        {
            "api_prefix": "/api/billing/",
            "code": "facturacion",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "ReceiptText",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Facturacion",
            "path": "/facturacion",
            "sort_order": 80,
        },
        {
            "api_prefix": "/api/billing/",
            "code": "cartas",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "FileText",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Cartas",
            "path": "/facturacion/cartas",
            "sort_order": 85,
        },
        {
            "api_prefix": "/api/meters",
            "code": "medidores",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Gauge",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Medidores",
            "path": "/medidores",
            "sort_order": 90,
        },
        {
            "api_prefix": "/api/inspections",
            "code": "inspecciones",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "SearchCheck",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Inspecciones",
            "path": "/inspecciones",
            "sort_order": 100,
        },
        {
            "api_prefix": "/api/supervision-records",
            "code": "ordenes",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "ClipboardList",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Ordenes",
            "path": "/ordenes",
            "sort_order": 110,
        },
        {
            "api_prefix": "/api/catalogs/",
            "code": "catalogos",
            "group_key": "gestion",
            "group_label": "GESTION",
            "icon": "BookOpenText",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Catalogos",
            "path": "/catalogos",
            "sort_order": 120,
        },
        {
            "api_prefix": "/api/readings",
            "code": "medicion",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Ruler",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Medicion",
            "path": "/medicion",
            "sort_order": 130,
        },
        {
            "api_prefix": "/api/pozos",
            "code": "pozos",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Droplets",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Pozos",
            "path": "/pozos",
            "sort_order": 140,
        },
    ],
    "supervisor": [
        {
            "api_prefix": "/api/gis/",
            "code": "mapa",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Map",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Mapa GIS",
            "path": "/mapa",
            "sort_order": 20,
        },
        {
            "api_prefix": "/api/pozos",
            "code": "pozos",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Droplets",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Pozos",
            "path": "/pozos",
            "sort_order": 30,
        },
        {
            "api_prefix": "/api/supervisiones",
            "code": "supervisiones",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "ClipboardPen",
            "is_active": True,
            "is_home_eligible": True,
            "label": "Supervisiones",
            "path": "/supervisiones",
            "sort_order": 40,
        },
        {
            "api_prefix": "/api/inspections",
            "code": "inspecciones",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "SearchCheck",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Inspecciones",
            "path": "/inspecciones",
            "sort_order": 50,
        },
        {
            "api_prefix": "/api/supervision-records",
            "code": "ordenes",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "ClipboardList",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Ordenes",
            "path": "/ordenes",
            "sort_order": 60,
        },
        {
            "api_prefix": "/api/meters",
            "code": "medidores",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Gauge",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Medidores",
            "path": "/medidores",
            "sort_order": 70,
        },
        {
            "api_prefix": "/api/readings",
            "code": "medicion",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "Ruler",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Medicion",
            "path": "/medicion",
            "sort_order": 80,
        },
        {
            "api_prefix": "/api/billing/",
            "code": "cartas",
            "group_key": "operacion",
            "group_label": "OPERACION",
            "icon": "FileText",
            "is_active": True,
            "is_home_eligible": False,
            "label": "Cartas",
            "path": "/facturacion/cartas",
            "sort_order": 85,
        },
    ],
}


def normalize_role_value(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized == "admin":
        return "admin"
    if normalized in {"operator", "supervisor"}:
        return "supervisor"
    return "supervisor"


def normalize_assignment_code(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


async def ensure_users_management_schema(pool: AsyncConnectionPool) -> None:
    global _users_schema_ready
    if _users_schema_ready:
        return

    try:
        await execute_statement(
            pool,
            """
            ALTER TABLE public.users
              ADD COLUMN IF NOT EXISTS assignment_code varchar(80);
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE INDEX IF NOT EXISTS idx_users_assignment_code
              ON public.users(assignment_code);
            """,
        )
        await execute_statement(
            pool,
            """
            UPDATE public.users
            SET role = 'supervisor'
            WHERE lower(role) = 'operator';
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE TABLE IF NOT EXISTS public.app_views (
              id bigserial PRIMARY KEY,
              code varchar(80) NOT NULL UNIQUE,
              label varchar(120) NOT NULL,
              path varchar(180) NOT NULL UNIQUE,
              api_prefix varchar(180),
              group_key varchar(80) NOT NULL,
              group_label varchar(120) NOT NULL,
              icon varchar(80),
              sort_order integer NOT NULL DEFAULT 0,
              is_active boolean NOT NULL DEFAULT TRUE,
              is_home_eligible boolean NOT NULL DEFAULT FALSE,
              created_at timestamptz NOT NULL DEFAULT NOW(),
              updated_at timestamptz NOT NULL DEFAULT NOW()
            );
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE INDEX IF NOT EXISTS idx_app_views_is_active
              ON public.app_views(is_active);
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE INDEX IF NOT EXISTS idx_app_views_sort_order
              ON public.app_views(sort_order);
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE TABLE IF NOT EXISTS public.user_view_permissions (
              user_id bigint NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
              view_id bigint NOT NULL REFERENCES public.app_views(id) ON DELETE CASCADE,
              granted_at timestamptz NOT NULL DEFAULT NOW(),
              granted_by_user_id bigint REFERENCES public.users(id) ON DELETE SET NULL,
              PRIMARY KEY (user_id, view_id)
            );
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE INDEX IF NOT EXISTS idx_user_view_permissions_user_id
              ON public.user_view_permissions(user_id);
            """,
        )
        await execute_statement(
            pool,
            """
            CREATE INDEX IF NOT EXISTS idx_user_view_permissions_view_id
              ON public.user_view_permissions(view_id);
            """,
        )
        _users_schema_ready = True
    except Exception as e:
        print(f"Warning: Non-fatal error while ensuring auth management schema: {e}")
        _users_schema_ready = True


async def find_user_by_identifier(
    pool: AsyncConnectionPool,
    identifier: str,
) -> dict | None:
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
            id,
            assignment_code,
            email,
            full_name,
            role,
            username,
            is_active
        FROM public.users
        WHERE lower(email) = %s OR lower(username) = %s
        LIMIT 1;
        """,
        [identifier, identifier],
    )
    return rows[0] if rows else None


async def find_user_for_login(
    pool: AsyncConnectionPool,
    identifier: str,
) -> dict | None:
    """Igual que `find_user_by_identifier` pero incluye `password_hash` --
    separado a proposito para que ese hash nunca salga por accidente de un
    endpoint que no sea el propio login."""
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT id, assignment_code, email, full_name, role, username, is_active, password_hash
        FROM public.users
        WHERE lower(email) = %s OR lower(username) = %s
        LIMIT 1;
        """,
        [identifier, identifier],
    )
    return rows[0] if rows else None


async def get_user_by_id(pool: AsyncConnectionPool, user_id: int) -> dict | None:
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
            u.id, u.username, u.email, u.full_name, u.role, u.is_active,
            u.assignment_code, u.last_login_at::text AS last_login_at,
            u.created_at::text AS created_at, u.updated_at::text AS updated_at,
            a.auth_user_id::text AS auth_user_id
        FROM public.users u
        LEFT JOIN public.auth_profiles_local a ON a.legacy_user_id = u.id
        WHERE u.id = %s
        LIMIT 1;
        """,
        [user_id],
    )
    return rows[0] if rows else None


async def get_user_credentials_by_id(pool: AsyncConnectionPool, user_id: int) -> dict | None:
    """Igual que `get_user_by_id` pero incluye `password_hash` -- solo para
    verificar la contrasena actual en el cambio de password self-service
    (`PATCH /auth/me/password`). Nunca exponer este resultado tal cual en una
    respuesta HTTP."""
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT id, is_active, password_hash
        FROM public.users
        WHERE id = %s
        LIMIT 1;
        """,
        [user_id],
    )
    return rows[0] if rows else None


async def touch_user_last_login(pool: AsyncConnectionPool, user_id: int) -> None:
    await ensure_users_management_schema(pool)
    await execute_statement(
        pool,
        """
        UPDATE public.users
        SET last_login_at = NOW(),
            updated_at = NOW()
        WHERE id = %s;
        """,
        [user_id],
    )


async def list_users(
    pool: AsyncConnectionPool,
    *,
    page: int,
    page_size: int,
    search: str | None,
    filter_view: str | None = None,
) -> dict:
    await ensure_users_management_schema(pool)

    conditions: list[str] = []
    params: list = []

    if search:
        params.append(f"%{search.strip().lower()}%")
        conditions.append(
            """
            lower(concat_ws(' ', username, email, full_name, assignment_code, role)) like %s
            """
        )

    if filter_view:
        params.append(filter_view.strip().lower())
        conditions.append(
            """EXISTS (
              SELECT 1 FROM public.user_view_permissions filter_uvp
              JOIN public.app_views filter_av ON filter_av.id = filter_uvp.view_id
              WHERE filter_uvp.user_id = u.id
                AND (lower(filter_av.code) = %s OR lower(filter_av.path) = %s)
            )"""
        )
        params.append(filter_view.strip().lower())

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    count_rows = await fetch_all_dict(
        pool,
        f"SELECT count(*)::int AS total FROM public.users u {where_clause};",
        params,
    )
    total = int(count_rows[0]["total"]) if count_rows else 0

    offset = (page - 1) * page_size
    rows = await fetch_all_dict(
        pool,
        f"""
        SELECT
            u.id,
            u.username,
            u.email,
            u.full_name,
            u.role,
            u.is_active,
            u.assignment_code,
            u.last_login_at::text AS last_login_at,
            u.created_at::text AS created_at,
            u.updated_at::text AS updated_at,
            COUNT(DISTINCT av.id)::int AS views_count
        FROM public.users u
        LEFT JOIN public.user_view_permissions uvp
          ON uvp.user_id = u.id
        LEFT JOIN public.app_views av
          ON av.id = uvp.view_id
         AND av.is_active = TRUE
        {where_clause}
        GROUP BY u.id
        ORDER BY u.full_name, u.username, u.id
        LIMIT %s OFFSET %s;
        """,
        [*params, page_size, offset],
    )

    return {
        "data": rows,
        "page": page,
        "pageSize": page_size,
        "total": total,
    }


async def create_user(
    pool: AsyncConnectionPool,
    *,
    email: str,
    username: str,
    full_name: str,
    password_hash: str,
    role: str,
    assignment_code: str | None,
    is_active: bool,
) -> dict | None:
    await ensure_users_management_schema(pool)
    try:
        async with pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO public.users (
                      email,
                      username,
                      full_name,
                      password_hash,
                      role,
                      assignment_code,
                      is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING
                      id,
                      username,
                      email,
                      full_name,
                      role,
                      is_active,
                      assignment_code,
                      last_login_at::text AS last_login_at,
                      created_at::text AS created_at,
                      updated_at::text AS updated_at;
                    """,
                    [
                        email,
                        username,
                        full_name,
                        password_hash,
                        normalize_role_value(role),
                        assignment_code,
                        is_active,
                    ],
                )
                row = await cursor.fetchone()
            await connection.commit()
        return row
    except UniqueViolation as err:
        msg = str(err).lower()
        if "username" in msg:
            raise HTTPException(status_code=409, detail=f"El nombre de usuario '{username}' ya está registrado.")
        if "email" in msg:
            raise HTTPException(status_code=409, detail=f"El correo '{email}' ya está registrado.")
        if "assignment_code" in msg:
            raise HTTPException(status_code=409, detail=f"El código de asignación '{assignment_code}' ya está en uso.")
        raise HTTPException(status_code=409, detail="Ya existe un usuario con estos datos.")


async def update_user(
    pool: AsyncConnectionPool,
    user_id: int,
    *,
    assignment_code: str | None,
    is_active: bool,
    role: str,
    email: str,
    full_name: str,
    username: str,
) -> dict | None:
    await ensure_users_management_schema(pool)
    try:
        await execute_statement(
            pool,
            """
            UPDATE public.users
            SET
                assignment_code = %s,
                is_active = %s,
                role = %s,
                email = %s,
                full_name = %s,
                username = %s,
                updated_at = NOW()
            WHERE id = %s;
            """,
            [
                assignment_code,
                is_active,
                normalize_role_value(role),
                email,
                full_name,
                username,
                user_id,
            ],
        )
        return await get_user_by_id(pool, user_id)
    except UniqueViolation as err:
        msg = str(err).lower()
        if "username" in msg:
            raise HTTPException(status_code=409, detail=f"El nombre de usuario '{username}' ya está registrado.")
        if "email" in msg:
            raise HTTPException(status_code=409, detail=f"El correo '{email}' ya está registrado.")
        if "assignment_code" in msg:
            raise HTTPException(status_code=409, detail=f"El código de asignación '{assignment_code}' ya está en uso.")
        raise HTTPException(status_code=409, detail="Ya existe un usuario con estos datos.")


async def update_user_password(
    pool: AsyncConnectionPool,
    user_id: int,
    *,
    password_hash: str,
) -> dict | None:
    await ensure_users_management_schema(pool)
    await execute_statement(
        pool,
        """
        UPDATE public.users
        SET
            password_hash = %s,
            updated_at = NOW()
        WHERE id = %s;
        """,
        [password_hash, user_id],
    )
    return await get_user_by_id(pool, user_id)


async def list_views(
    pool: AsyncConnectionPool,
    *,
    include_inactive: bool = True,
) -> list[dict]:
    await ensure_users_management_schema(pool)
    params: list = []
    where_clause = ""
    if not include_inactive:
        where_clause = "WHERE is_active = TRUE"

    return await fetch_all_dict(
        pool,
        f"""
        SELECT
          id,
          code,
          label,
          path,
          api_prefix,
          group_key,
          group_label,
          icon,
          sort_order,
          is_active,
          is_home_eligible,
          created_at::text AS created_at,
          updated_at::text AS updated_at
        FROM public.app_views
        {where_clause}
        ORDER BY group_label, sort_order, label, id;
        """,
        params,
    )


async def get_view_by_id(pool: AsyncConnectionPool, view_id: int) -> dict | None:
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
          id,
          code,
          label,
          path,
          api_prefix,
          group_key,
          group_label,
          icon,
          sort_order,
          is_active,
          is_home_eligible,
          created_at::text AS created_at,
          updated_at::text AS updated_at
        FROM public.app_views
        WHERE id = %s
        LIMIT 1;
        """,
        [view_id],
    )
    return rows[0] if rows else None


async def create_view(
    pool: AsyncConnectionPool,
    *,
    code: str,
    label: str,
    path: str,
    api_prefix: str | None,
    group_key: str,
    group_label: str,
    icon: str | None,
    sort_order: int,
    is_active: bool,
    is_home_eligible: bool,
) -> dict | None:
    await ensure_users_management_schema(pool)
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO public.app_views (
                  code,
                  label,
                  path,
                  api_prefix,
                  group_key,
                  group_label,
                  icon,
                  sort_order,
                  is_active,
                  is_home_eligible
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING
                  id,
                  code,
                  label,
                  path,
                  api_prefix,
                  group_key,
                  group_label,
                  icon,
                  sort_order,
                  is_active,
                  is_home_eligible,
                  created_at::text AS created_at,
                  updated_at::text AS updated_at;
                """,
                [
                    code,
                    label,
                    path,
                    api_prefix,
                    group_key,
                    group_label,
                    icon,
                    sort_order,
                    is_active,
                    is_home_eligible,
                ],
            )
            row = await cursor.fetchone()
        await connection.commit()
    return row


async def update_view(
    pool: AsyncConnectionPool,
    view_id: int,
    *,
    code: str,
    label: str,
    path: str,
    api_prefix: str | None,
    group_key: str,
    group_label: str,
    icon: str | None,
    sort_order: int,
    is_active: bool,
    is_home_eligible: bool,
) -> dict | None:
    await ensure_users_management_schema(pool)
    await execute_statement(
        pool,
        """
        UPDATE public.app_views
        SET
          code = %s,
          label = %s,
          path = %s,
          api_prefix = %s,
          group_key = %s,
          group_label = %s,
          icon = %s,
          sort_order = %s,
          is_active = %s,
          is_home_eligible = %s,
          updated_at = NOW()
        WHERE id = %s;
        """,
        [
            code,
            label,
            path,
            api_prefix,
            group_key,
            group_label,
            icon,
            sort_order,
            is_active,
            is_home_eligible,
            view_id,
        ],
    )
    return await get_view_by_id(pool, view_id)


async def get_user_views(
    pool: AsyncConnectionPool,
    user_id: int,
) -> dict:
    await ensure_users_management_schema(pool)
    views = await list_views(pool, include_inactive=True)
    assigned_rows = await fetch_all_dict(
        pool,
        """
        SELECT view_id
        FROM public.user_view_permissions
        WHERE user_id = %s;
        """,
        [user_id],
    )
    assigned_view_ids = [int(row["view_id"]) for row in assigned_rows]
    return {
        "assignedViewIds": assigned_view_ids,
        "views": views,
    }


async def replace_user_views(
    pool: AsyncConnectionPool,
    user_id: int,
    *,
    view_ids: list[int],
    granted_by_user_id: int | None,
) -> dict:
    await ensure_users_management_schema(pool)
    unique_view_ids = sorted({int(view_id) for view_id in view_ids})

    async with pool.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM public.user_view_permissions WHERE user_id = %s;",
                [user_id],
            )
            if unique_view_ids:
                await cursor.executemany(
                    """
                    INSERT INTO public.user_view_permissions (user_id, view_id, granted_by_user_id)
                    VALUES (%s, %s, %s);
                    """,
                    [(user_id, view_id, granted_by_user_id) for view_id in unique_view_ids],
                )
        await connection.commit()

    return await get_user_views(pool, user_id)


def build_legacy_access_snapshot(role: str | None) -> dict:
    normalized_role = normalize_role_value(role)
    fallback_views = LEGACY_FALLBACK_VIEWS.get(normalized_role, LEGACY_FALLBACK_VIEWS["supervisor"])
    allowed_paths = [view["path"] for view in fallback_views if view.get("path")]
    allowed_api_prefixes = [view["api_prefix"] for view in fallback_views if view.get("api_prefix")]
    home_path = next(
        (view["path"] for view in fallback_views if view.get("is_home_eligible") and view.get("path")),
        allowed_paths[0] if allowed_paths else "/login",
    )
    return {
        "allowedApiPrefixes": allowed_api_prefixes,
        "allowedPaths": allowed_paths,
        "homePath": home_path,
        "isFallback": True,
        "menuGroups": build_menu_groups(fallback_views),
        "views": fallback_views,
    }


def build_menu_groups(views: list[dict]) -> list[dict]:
    groups: "OrderedDict[str, dict]" = OrderedDict()

    for view in sorted(
        [view for view in views if view.get("is_active", True) and view.get("path")],
        key=lambda item: (
            str(item.get("group_label") or ""),
            int(item.get("sort_order") or 0),
            str(item.get("label") or ""),
            int(item.get("id") or 0),
        ),
    ):
        group_key = str(view.get("group_key") or "general")
        group = groups.setdefault(
            group_key,
            {
                "dot": "bg-sky-500" if len(groups) == 0 else "bg-violet-500",
                "items": [],
                "title": str(view.get("group_label") or "GENERAL"),
            },
        )
        group["items"].append(
            {
                "icon": view.get("icon") or "LayoutDashboard",
                "label": view.get("label"),
                "path": view.get("path"),
                "subItems": [],
            }
        )

    return list(groups.values())


async def get_access_snapshot(
    pool: AsyncConnectionPool,
    *,
    user_id: int,
    role: str | None,
) -> dict:
    await ensure_users_management_schema(pool)
    rows = await fetch_all_dict(
        pool,
        """
        SELECT
          av.id,
          av.code,
          av.label,
          av.path,
          av.api_prefix,
          av.group_key,
          av.group_label,
          av.icon,
          av.sort_order,
          av.is_active,
          av.is_home_eligible
        FROM public.user_view_permissions uvp
        INNER JOIN public.app_views av
          ON av.id = uvp.view_id
        WHERE uvp.user_id = %s
          AND av.is_active = TRUE
        ORDER BY av.group_label, av.sort_order, av.label, av.id;
        """,
        [user_id],
    )

    if not rows:
        return build_legacy_access_snapshot(role)

    allowed_paths = [row["path"] for row in rows if row.get("path")]
    allowed_api_prefixes = [row["api_prefix"] for row in rows if row.get("api_prefix")]
    home_path = next(
        (row["path"] for row in rows if row.get("is_home_eligible") and row.get("path")),
        allowed_paths[0] if allowed_paths else "/login",
    )

    return {
        "allowedApiPrefixes": allowed_api_prefixes,
        "allowedPaths": allowed_paths,
        "homePath": home_path,
        "isFallback": False,
        "menuGroups": build_menu_groups(rows),
        "views": rows,
    }
