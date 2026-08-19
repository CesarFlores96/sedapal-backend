"""
Mapeo bidireccional entre la estructura editable de la app movil
(section4WaterConnection / section5SewerBox / meterRows) y las columnas
planas "estilo catalogo" de `public.supervision` (misma tabla que usa
la tabla editable web y el generador de PDF).

Reemplaza al antiguo `supervision_records` (JSONB en tabla aparte):
ahora hay una unica fila por `num_os` (work_order_number) y una unica
fuente de verdad para PDF, web y movil.

Nota: durante la migracion se detectaron 3 discrepancias entre el mapeo
anterior (usado solo para render de PDF, nunca escrito a BD) y las
columnas reales de `supervision`:
  - "seg_disp" no existe; la columna real es "disp_seg".
  - "est_med" no existe; el estado del medidor por conexion vive en
    fun_med1/fun_med2/fun_med3 (uno por fila de meterRows), no en un
    campo global "meterState" (que la UI movil nunca llena).
  - "pos_clan" no existe; la columna real es "clandes".
Este modulo usa los nombres reales, verificados contra information_schema.
"""

from decimal import Decimal, InvalidOperation


# Expresion SQL canonica de la "fecha de visita" de una supervision.
# `fec_emis` (emision de la OT, texto DD/MM/YYYY) es la fuente de verdad de la
# fecha que muestran movil, web y PDF. Se centraliza aqui para que el endpoint
# de agenda (supervisions.py) y el de la tabla web (supervision_table.py)
# deriven SIEMPRE la misma fecha y no vuelvan a divergir (antes la web usaba
# `fec_vis` crudo, con formatos mezclados).
VISIT_DATE_SQL = "to_date(NULLIF(s.fec_emis, ''), 'DD/MM/YYYY')"
VISIT_DATE_SQL_NO_ALIAS = "to_date(NULLIF(fec_emis, ''), 'DD/MM/YYYY')"


def safe_numeric(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def text_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_connection_code(row: dict | None) -> str | None:
    if not row:
        return None
    if row.get("nipple"):
        return "CE002"
    if row.get("snm"):
        return "CE003"
    return None


# Columnas de `supervision` que este mapeo escribe/lee. Usado para construir
# el UPDATE ... SET de save_supervision.
FLAT_COLUMNS: list[str] = [
    "abast", "caja", "est_caja", "est_tapa", "dia_conex_agua", "seguro_tap",
    "disp_seg", "serv", "fuga_caj", "imposibilidad", "clandes", "inci_medidor",
    "observm1",
    "dia_med", "medidor", "lec", "fun_med1", "cnx_con",
    "dia_med2", "med2", "lec_med2", "fun_med2", "cnx2_con",
    "dia_med3", "med3", "lec_med3", "fun_med3", "cnx3_con",
    "cod_ubicc", "dia_conex_des", "est_caja_des", "est_tapa_des", "est_tub_des",
    "ind_tram_des", "prof_cja_des", "long_cnx_des", "observm2", "sis_trat_res",
    "tip_des_des", "tip_mat_tap_d", "tip_mat_tub_d", "tip_suelo_des",
]


def section_to_flat_columns(section4: dict, section5: dict) -> dict:
    meter_rows = section4.get("meterRows") or []
    row1 = meter_rows[0] if len(meter_rows) > 0 else {}
    row2 = meter_rows[1] if len(meter_rows) > 1 else {}
    row3 = meter_rows[2] if len(meter_rows) > 2 else {}
    meter_incidents = [i for i in (section4.get("meterIncidents") or []) if i]

    return {
        "abast": text_or_none(section4.get("waterSupplyAvailable")),
        "caja": text_or_none(section4.get("boxLocation")),
        "est_caja": text_or_none(section4.get("boxState")),
        "est_tapa": text_or_none(section4.get("lidState")),
        "dia_conex_agua": safe_numeric(section4.get("connectionDiameter")),
        "seguro_tap": text_or_none(section4.get("coverSecurityType")),
        "disp_seg": text_or_none(section4.get("securityDevice")),
        "serv": text_or_none(section4.get("serviceCondition")),
        "fuga_caj": text_or_none(section4.get("boxLeakStatus")),
        "imposibilidad": text_or_none(section4.get("boxImpossibility")),
        "clandes": text_or_none(section4.get("possibleClandestine")),
        "inci_medidor": "; ".join(meter_incidents) if meter_incidents else None,
        "observm1": text_or_none(section4.get("observations")),
        "dia_med": text_or_none(row1.get("diameter")),
        "medidor": text_or_none(row1.get("meterNumber")),
        "lec": text_or_none(row1.get("reading")),
        "fun_med1": text_or_none(row1.get("meterState")),
        "cnx_con": resolve_connection_code(row1),
        "dia_med2": text_or_none(row2.get("diameter")),
        "med2": text_or_none(row2.get("meterNumber")),
        "lec_med2": safe_numeric(row2.get("reading")),
        "fun_med2": text_or_none(row2.get("meterState")),
        "cnx2_con": resolve_connection_code(row2),
        "dia_med3": text_or_none(row3.get("diameter")),
        "med3": text_or_none(row3.get("meterNumber")),
        "lec_med3": safe_numeric(row3.get("reading")),
        "fun_med3": text_or_none(row3.get("meterState")),
        "cnx3_con": resolve_connection_code(row3),
        "cod_ubicc": text_or_none(section5.get("registerBoxLocation")),
        "dia_conex_des": text_or_none(section5.get("diameter")),
        "est_caja_des": text_or_none(section5.get("registerBoxState")),
        "est_tapa_des": text_or_none(section5.get("coverState")),
        "est_tub_des": text_or_none(section5.get("pipeState")),
        "ind_tram_des": text_or_none(section5.get("greaseTrap")),
        "prof_cja_des": safe_numeric(section5.get("depth")),
        "long_cnx_des": safe_numeric(section5.get("boxNetworkLength")),
        "observm2": text_or_none(section5.get("observations")),
        "sis_trat_res": text_or_none(section5.get("treatmentSystem")),
        "tip_des_des": text_or_none(section5.get("dischargeType")),
        "tip_mat_tap_d": text_or_none(section5.get("coverMaterial")),
        "tip_mat_tub_d": text_or_none(section5.get("pipeMaterial")),
        "tip_suelo_des": text_or_none(section5.get("soilType")),
    }


def _connection_flags(code: str | None) -> tuple[bool, bool]:
    return (code == "CE002", code == "CE003")


def flat_columns_to_sections(row: dict) -> tuple[dict, dict]:
    meter_rows = []
    for label, dia_key, num_key, lec_key, state_key, cnx_key in [
        ("1ra. Cnx", "dia_med", "medidor", "lec", "fun_med1", "cnx_con"),
        ("2da. Cnx", "dia_med2", "med2", "lec_med2", "fun_med2", "cnx2_con"),
        ("3ra. Cnx", "dia_med3", "med3", "lec_med3", "fun_med3", "cnx3_con"),
    ]:
        nipple, snm = _connection_flags(row.get(cnx_key))
        reading = row.get(lec_key)
        meter_rows.append({
            "diameter": row.get(dia_key),
            "label": label,
            "meterNumber": row.get(num_key),
            "meterState": row.get(state_key),
            "nipple": nipple,
            "reading": str(reading) if reading is not None else None,
            "snm": snm,
        })

    meter_incidents = [
        item.strip() for item in (row.get("inci_medidor") or "").split(";") if item.strip()
    ]

    dia_conex_agua = row.get("dia_conex_agua")
    prof_cja_des = row.get("prof_cja_des")
    long_cnx_des = row.get("long_cnx_des")

    section4 = {
        "boxImpossibility": row.get("imposibilidad"),
        "boxLeakStatus": row.get("fuga_caj"),
        "boxLocation": row.get("caja"),
        "boxState": row.get("est_caja"),
        "connectionDiameter": str(dia_conex_agua) if dia_conex_agua is not None else None,
        "coverSecurityType": row.get("seguro_tap"),
        "lidState": row.get("est_tapa"),
        "meterIncidents": meter_incidents,
        "meterRows": meter_rows,
        "observations": row.get("observm1"),
        "possibleClandestine": row.get("clandes"),
        "securityDevice": row.get("disp_seg"),
        "serviceCondition": row.get("serv"),
        "waterSupplyAvailable": row.get("abast"),
    }

    section5 = {
        "boxNetworkLength": str(long_cnx_des) if long_cnx_des is not None else None,
        "coverMaterial": row.get("tip_mat_tap_d"),
        "coverState": row.get("est_tapa_des"),
        "depth": str(prof_cja_des) if prof_cja_des is not None else None,
        "diameter": row.get("dia_conex_des"),
        "dischargeType": row.get("tip_des_des"),
        "greaseTrap": row.get("ind_tram_des"),
        "observations": row.get("observm2"),
        "pipeMaterial": row.get("tip_mat_tub_d"),
        "pipeState": row.get("est_tub_des"),
        "registerBoxLocation": row.get("cod_ubicc"),
        "registerBoxState": row.get("est_caja_des"),
        "soilType": row.get("tip_suelo_des"),
        "treatmentSystem": row.get("sis_trat_res"),
    }

    return section4, section5
