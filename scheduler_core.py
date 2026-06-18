"""
scheduler_core.py – Motor de horarios Bembos
=============================================
Correcciones aplicadas:
- parse_hhmm: "1:00" = 01:00 (1 AM), nunca 1 minuto.
- Horas mostradas en formato HH:MM, sin decimales.
- PT máximo estricto: 19:00 h (1140 min). FT: 48:00 h (2880 min).
- Apertura: turno que empieza a las 07:00 u 08:00.
- Cierre: turno que cruza medianoche y termina a las 01:00 o después.
- Mínimos de cierre: 3 PRODUCCION + 2 SERVICIO por noche de cierre.
- Solicitud de descanso → libera el día y redistribuye horas en otro día disponible.
- Nuevo trabajador → se asigna según disponibilidad y área, completando horas.
- Baja → se marca inactivo; si era cierre/apertura, se asigna sustituto.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

DAYS = ["LUNES", "MARTES", "MIERCOLES", "JUEVES", "VIERNES", "SABADO", "DOMINGO"]

DAY_ALIASES = {
    "LUNES": "LUNES", "LU": "LUNES", "L": "LUNES",
    "MARTES": "MARTES", "MA": "MARTES",
    "MIERCOLES": "MIERCOLES", "MIÉRCOLES": "MIERCOLES", "MI": "MIERCOLES", "X": "MIERCOLES",
    "JUEVES": "JUEVES", "JU": "JUEVES", "J": "JUEVES",
    "VIERNES": "VIERNES", "VI": "VIERNES", "V": "VIERNES",
    "SABADO": "SABADO", "SÁBADO": "SABADO", "SA": "SABADO", "S": "SABADO",
    "DOMINGO": "DOMINGO", "DO": "DOMINGO", "D": "DOMINGO",
}

AREA_ALIASES = {
    "SERVICIO": "SERVICIO", "SVC": "SERVICIO", "SERVICE": "SERVICIO",
    "PRODUCCION": "PRODUCCION", "PRODUCCIÓN": "PRODUCCION",
    "PROD": "PRODUCCION", "COCINA": "PRODUCCION",
}

CONTRACT_ALIASES = {
    "PT": "PT", "PART TIME": "PT", "PART-TIME": "PT", "PARTTIME": "PT",
    "FT": "FT", "FULL TIME": "FT", "FULL-TIME": "FT", "FULLTIME": "FT",
}

OFF_TOKENS = {"", "OFF", "NULL", "NULO", "LIBRE", "DESCANSO", "DESC", "NA", "N/A", "-"}

# Límites estrictos en minutos
PT_MAX_MINUTES = 19 * 60        # 1140 min = 19:00 h exactas
FT_MAX_MINUTES = 48 * 60        # 2880 min = 48:00 h exactas
PT_TARGET_MINUTES = 19 * 60
FT_TARGET_MINUTES = 48 * 60

# Apertura: turno que comienza a las 07:00 u 08:00
OPENING_HOURS = {7 * 60, 8 * 60}   # 420, 480 minutos

# Cierre: turno que cruza medianoche y termina entre 01:00 y 04:00
CLOSE_FROM_MIN = 1 * 60   # 60 min (01:00)
CLOSE_TO_MIN   = 4 * 60   # 240 min (04:00)

# Mínimos de cierre por área
MIN_CIERRE_PRODUCCION = 3
MIN_CIERRE_SERVICIO   = 2


@dataclass
class Shift:
    start_min: int
    end_min: int
    raw: str

    @property
    def crosses_midnight(self) -> bool:
        return self.end_min <= self.start_min

    @property
    def raw_hours(self) -> float:
        duration = self.end_min - self.start_min
        if duration <= 0:
            duration += 24 * 60
        return duration / 60

    @property
    def duration_minutes(self) -> int:
        d = self.end_min - self.start_min
        if d <= 0:
            d += 24 * 60
        return d


def normalize_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    txt = str(value).strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt


def normalize_key(value) -> str:
    txt = normalize_text(value).upper()
    replacements = str.maketrans({"Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ñ": "N"})
    return txt.translate(replacements)


def normalize_day(value) -> str:
    key = normalize_key(value)
    return DAY_ALIASES.get(key, key)


def normalize_area(value) -> str:
    key = normalize_key(value)
    return AREA_ALIASES.get(key, key)


def normalize_contract(value) -> str:
    key = normalize_key(value)
    return CONTRACT_ALIASES.get(key, "")


def is_off_cell(value) -> bool:
    txt = normalize_key(value)
    if txt in OFF_TOKENS:
        return True
    return bool(txt) and txt.replace(" ", "") in {"NULLNUL", "NULLNULL", "NULNUL"}


def parse_hhmm(value: str) -> int:
    """
    Convierte una cadena HH:MM o H:MM a minutos desde medianoche.
    IMPORTANTE: "1:00" = 01:00 (1 AM = 60 minutos), NO 1 minuto.
    Rango válido: 0..23 para horas, 0..59 para minutos.
    """
    txt = normalize_text(value)
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", txt)
    if not match:
        raise ValueError(f"Hora inválida: {value!r}")
    hour   = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 0 or hour > 23:
        raise ValueError(f"Hora fuera de rango (0-23): {value!r}")
    if minute < 0 or minute > 59:
        raise ValueError(f"Minutos fuera de rango (0-59): {value!r}")
    return hour * 60 + minute


def minutes_to_hhmm(minutes: int) -> str:
    """Devuelve HH:MM (siempre dos dígitos en cada parte)."""
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_paid_hours(minutes: int) -> str:
    """Devuelve las horas pagadas en formato HH:MM sin decimales."""
    minutes = max(0, int(round(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_shift_cell(value) -> Optional[Shift]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if is_off_cell(value):
        return None
    txt = normalize_text(value)
    txt = txt.replace("–", "-").replace("—", "-")
    if "-" not in txt:
        return None
    parts = txt.split("-", 1)
    left, right = parts[0].strip(), parts[1].strip()
    try:
        start = parse_hhmm(left)
        end   = parse_hhmm(right)
    except ValueError:
        return None
    return Shift(start, end, f"{minutes_to_hhmm(start)} - {minutes_to_hhmm(end)}")


def shift_paid_minutes(shift: Optional[Shift], break_after_hours: float = 6.0, break_minutes: int = 45) -> int:
    """
    Retorna los minutos que se computan para el total semanal del trabajador.

    REGLA BEMBOS: el refrigerio NO se descuenta de las horas pagadas.
    El trabajador tiene derecho al break pero sus horas totales se cuentan
    sobre la duración completa del turno (entrada → salida).

    Los parámetros break_after_hours y break_minutes se mantienen por
    compatibilidad de firma pero NO afectan el resultado.
    """
    if shift is None:
        return 0
    return shift.duration_minutes   # duración bruta, sin descontar break


def shift_paid_hours(shift: Optional[Shift], break_after_hours: float = 6.0, break_minutes: int = 45) -> float:
    """Retorna las horas pagadas como float (para compatibilidad interna)."""
    return shift_paid_minutes(shift, break_after_hours, break_minutes) / 60.0


def is_opening_shift(shift: Optional[Shift]) -> bool:
    """Apertura: turno que empieza a las 07:00 u 08:00."""
    if shift is None:
        return False
    return shift.start_min in OPENING_HOURS


def shift_closes(shift: Optional[Shift], close_from_hour: int = 1, close_to_hour: int = 4) -> bool:
    """
    Cierre: turno que cruza medianoche y termina entre close_from_hour y close_to_hour.
    Ejemplo: 16:15 - 01:00 → cierre. 15:00 - 23:00 → NO cierre.
    """
    if shift is None:
        return False
    if shift.end_min > shift.start_min:   # no cruza medianoche
        return False
    end = shift.end_min % (24 * 60)
    return close_from_hour * 60 <= end <= close_to_hour * 60


def format_shift(shift: Optional[Shift]) -> str:
    return shift.raw if shift else "OFF"


def _find_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    normalized = {normalize_key(c): c for c in df.columns}
    for candidate in candidates:
        key = normalize_key(candidate)
        if key in normalized:
            return normalized[key]
    return None


def read_excel_any(file_or_path) -> pd.DataFrame:
    xls = pd.ExcelFile(file_or_path)
    lower_map = {str(s).lower().strip(): s for s in xls.sheet_names}
    if "horarios" in lower_map:
        sheet = lower_map["horarios"]
    elif "horario_base" in lower_map:
        sheet = lower_map["horario_base"]
    else:
        sheet = xls.sheet_names[0]
    # dtype=object para preservar strings "1:00" sin convertirlos a time
    return pd.read_excel(file_or_path, sheet_name=sheet, dtype=object)


def normalize_input_excel(file_or_path, settings: Optional[dict] = None) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Devuelve (employees_df, schedule_long_df, wide_df)."""
    settings = settings or {}
    break_after_hours = float(settings.get("break_after_hours", 6.0))
    break_minutes     = int(settings.get("break_minutes", 45))
    ft_threshold      = float(settings.get("ft_infer_threshold", 35.0))
    close_from_hour   = int(settings.get("close_from_hour", 1))
    close_to_hour     = int(settings.get("close_to_hour", 4))

    raw_df = read_excel_any(file_or_path)
    raw_df = raw_df.dropna(how="all")
    raw_df.columns = [normalize_text(c) for c in raw_df.columns]

    worker_col = _find_column(raw_df, ["COLABORADOR", "TRABAJADOR", "NOMBRE", "EMPLEADO"])
    area_col   = _find_column(raw_df, ["AREA", "ÁREA"])
    turno_col  = _find_column(raw_df, ["TURNO", "TIPO", "CONTRATO", "PT/FT"])
    day_cols   = {day: _find_column(raw_df, [day]) for day in DAYS}

    dia_col    = _find_column(raw_df, ["DIA", "DÍA"])
    inicio_col = _find_column(raw_df, ["HORA INICIO", "INICIO", "ENTRADA", "HORA ENTRADA"])
    fin_col    = _find_column(raw_df, ["HORA FIN", "FIN", "SALIDA", "HORA SALIDA"])

    if worker_col is None or area_col is None:
        raise ValueError("El Excel debe tener columnas de colaborador/trabajador y área.")

    rows: List[dict] = []

    if dia_col and inicio_col and fin_col:
        # Formato largo
        for _, row in raw_df.iterrows():
            name     = normalize_text(row.get(worker_col))
            if not name:
                continue
            area     = normalize_area(row.get(area_col))
            contract = normalize_contract(row.get(turno_col)) if turno_col else ""
            day      = normalize_day(row.get(dia_col))
            start_raw = normalize_text(row.get(inicio_col))
            end_raw   = normalize_text(row.get(fin_col))
            if day not in DAYS:
                continue
            shift = None
            if start_raw and end_raw and not is_off_cell(start_raw) and not is_off_cell(end_raw):
                shift = parse_shift_cell(f"{start_raw} - {end_raw}")
            paid_min = shift_paid_minutes(shift, break_after_hours, break_minutes)
            rows.append({
                "Trabajador":    name,
                "Area":          area,
                "Turno":         contract,
                "Dia":           day,
                "Shift":         format_shift(shift),
                "Hora Inicio":   minutes_to_hhmm(shift.start_min) if shift else "",
                "Hora Fin":      minutes_to_hhmm(shift.end_min) if shift else "",
                "Minutos Brutos": shift.duration_minutes if shift else 0,
                "Minutos Pagados": paid_min,
                "Horas Pagadas": round(paid_min / 60, 4),
                "Cierre":        shift_closes(shift, close_from_hour, close_to_hour),
                "Apertura":      is_opening_shift(shift),
            })
    else:
        if not all(day_cols.values()):
            missing = [d for d, c in day_cols.items() if c is None]
            raise ValueError(f"Faltan columnas de día: {', '.join(missing)}")
        for _, row in raw_df.iterrows():
            name     = normalize_text(row.get(worker_col))
            if not name:
                continue
            area     = normalize_area(row.get(area_col))
            contract = normalize_contract(row.get(turno_col)) if turno_col else ""
            for day, col in day_cols.items():
                shift    = parse_shift_cell(row.get(col))
                paid_min = shift_paid_minutes(shift, break_after_hours, break_minutes)
                rows.append({
                    "Trabajador":    name,
                    "Area":          area,
                    "Turno":         contract,
                    "Dia":           day,
                    "Shift":         format_shift(shift),
                    "Hora Inicio":   minutes_to_hhmm(shift.start_min) if shift else "",
                    "Hora Fin":      minutes_to_hhmm(shift.end_min) if shift else "",
                    "Minutos Brutos": shift.duration_minutes if shift else 0,
                    "Minutos Pagados": paid_min,
                    "Horas Pagadas": round(paid_min / 60, 4),
                    "Cierre":        shift_closes(shift, close_from_hour, close_to_hour),
                    "Apertura":      is_opening_shift(shift),
                })

    long_df = pd.DataFrame(rows)
    if long_df.empty:
        raise ValueError("No se detectaron turnos válidos en el Excel.")

    # Inferir empleados
    employee_base = (
        long_df.groupby(["Trabajador", "Area"], as_index=False)
        .agg({
            "Minutos Pagados": "sum",
            "Dia": "count",
            "Turno": lambda s: next((x for x in s if normalize_contract(x)), ""),
        })
    )
    worked_days = long_df[long_df["Shift"] != "OFF"].groupby("Trabajador")["Dia"].nunique().to_dict()

    employee_base["Minutos Semana Base"] = employee_base["Minutos Pagados"]
    employee_base["Horas Semana Base"]   = (employee_base["Minutos Pagados"] / 60).round(2)
    employee_base["Dias Trabajados Base"] = employee_base["Trabajador"].map(worked_days).fillna(0).astype(int)
    employee_base["Descansos Base"]       = 7 - employee_base["Dias Trabajados Base"]
    employee_base["Turno"] = employee_base.apply(
        lambda r: normalize_contract(r["Turno"]) or ("FT" if r["Horas Semana Base"] >= ft_threshold else "PT"),
        axis=1,
    )
    employee_base["Activo"]      = True
    employee_base["Max Minutos"] = employee_base["Turno"].map({"PT": PT_MAX_MINUTES, "FT": FT_MAX_MINUTES}).fillna(PT_MAX_MINUTES)
    employee_base["Max Horas"]   = employee_base["Turno"].map({"PT": 19.0, "FT": 48.0}).fillna(19.0)
    employee_base["Min Descansos"] = employee_base["Turno"].map({"PT": 2, "FT": 1}).fillna(2).astype(int)

    employees_df = employee_base[[
        "Trabajador", "Area", "Turno", "Activo",
        "Max Horas", "Max Minutos", "Min Descansos",
        "Horas Semana Base", "Dias Trabajados Base", "Descansos Base",
    ]]

    wide_df = long_to_wide(long_df)
    return employees_df, long_df, wide_df


def long_to_wide(long_df: pd.DataFrame) -> pd.DataFrame:
    base  = long_df[["Trabajador", "Area", "Turno"]].drop_duplicates("Trabajador")
    pivot = long_df.pivot_table(index="Trabajador", columns="Dia", values="Shift", aggfunc="first")
    pivot = pivot.reindex(columns=DAYS)
    wide  = base.merge(pivot.reset_index(), on="Trabajador", how="left")
    for day in DAYS:
        wide[day] = wide[day].fillna("OFF")
    return wide[["Trabajador", "Area", "Turno"] + DAYS]


def parse_shift_string(value: str) -> Optional[Shift]:
    return parse_shift_cell(value)


def build_requirements_from_base(long_df: pd.DataFrame, close_from_hour: int = 1, close_to_hour: int = 4) -> pd.DataFrame:
    work = long_df[long_df["Shift"] != "OFF"].copy()
    if work.empty:
        return pd.DataFrame(columns=["Dia", "Area", "Min Personas", "Min Cierres"])

    work["Cierre"] = work["Shift"].map(
        lambda s: shift_closes(parse_shift_string(s), close_from_hour, close_to_hour)
    )
    closers = work[work["Cierre"]].copy()

    rows = []
    for day in DAYS:
        for area in ["SERVICIO", "PRODUCCION"]:
            # Mínimos de cierre fijos según regla de negocio
            min_cierre = MIN_CIERRE_PRODUCCION if area == "PRODUCCION" else MIN_CIERRE_SERVICIO
            count = int(closers[
                (closers["Dia"] == day) &
                (closers["Area"].map(normalize_area) == area)
            ]["Trabajador"].nunique())
            rows.append({
                "Dia":         day,
                "Area":        area,
                "Min Personas": count,
                "Min Cierres":  max(count, min_cierre),
            })

    grouped = pd.DataFrame(rows)
    grouped["Dia"] = pd.Categorical(grouped["Dia"], categories=DAYS, ordered=True)
    return grouped.sort_values(["Dia", "Area"]).reset_index(drop=True)


def build_availability_from_base(long_df: pd.DataFrame) -> pd.DataFrame:
    av = long_df[long_df["Shift"] != "OFF"][["Trabajador", "Dia", "Hora Inicio", "Hora Fin"]].copy()
    av["Disponible"] = True
    return av.reset_index(drop=True)


def build_shift_pool(long_df: pd.DataFrame, close_from_hour: int = 1, close_to_hour: int = 4) -> Dict[Tuple[str, str, bool], List[Shift]]:
    pool: Dict[Tuple[str, str, bool], List[Shift]] = {}
    work = long_df[long_df["Shift"] != "OFF"].copy()
    for (area, day, cierre, shift_str), grp in work.groupby(["Area", "Dia", "Cierre", "Shift"]):
        shift = parse_shift_string(shift_str)
        if shift:
            key = (normalize_area(area), day, bool(cierre))
            pool.setdefault(key, [])
            pool[key].extend([shift] * len(grp))
    for key in list(pool):
        freq = {}
        for sh in pool[key]:
            freq[sh.raw] = freq.get(sh.raw, 0) + 1
        unique = [parse_shift_string(k) for k, _ in sorted(freq.items(), key=lambda x: -x[1])]
        pool[key] = [x for x in unique if x]
    return pool


def make_shift(start: str, end: str) -> Shift:
    s = parse_hhmm(start)
    e = parse_hhmm(end)
    return Shift(s, e, f"{minutes_to_hhmm(s)} - {minutes_to_hhmm(e)}")


def availability_intervals(availability_df: pd.DataFrame, employee: str, day: str) -> List[Tuple[int, int]]:
    if availability_df is None or availability_df.empty:
        return [(0, 24 * 60)]
    df   = availability_df.copy().fillna("")
    rows = df[
        (df["Trabajador"].astype(str).str.upper() == employee.upper()) &
        (df["Dia"].astype(str).map(normalize_day) == day)
    ]
    if rows.empty:
        return []
    intervals: List[Tuple[int, int]] = []
    for _, row in rows.iterrows():
        available_raw = row.get("Disponible", True)
        available     = bool(available_raw)
        if normalize_key(str(available_raw)) in {"FALSE", "0", "NO", "N", "OFF", "NO DISPONIBLE"}:
            available = False
        if not available:
            continue
        start = normalize_text(row.get("Hora Inicio"))
        end   = normalize_text(row.get("Hora Fin"))
        if not start or not end or is_off_cell(start) or is_off_cell(end):
            continue
        try:
            s = parse_hhmm(start)
            e = parse_hhmm(end)
        except ValueError:
            continue
        if e <= s:
            e += 24 * 60
        intervals.append((s, e))
    return intervals


def shift_fits_availability(shift: Shift, employee: str, day: str, availability_df: pd.DataFrame) -> bool:
    if availability_df is None or availability_df.empty:
        return True
    s1, e1 = shift.start_min, shift.end_min
    if e1 <= s1:
        e1 += 24 * 60
    for s2, e2 in availability_intervals(availability_df, employee, day):
        if s1 >= s2 and e1 <= e2:
            return True
    return False


def dynamic_shift_from_availability(
    employee: str,
    day: str,
    availability_df: pd.DataFrame,
    desired_paid_minutes: int,
    settings: dict,
    closing: bool = False,
    min_paid_minutes: int = 120,
) -> Optional[Shift]:
    """Crea un turno dentro de la disponibilidad para completar minutos objetivo."""
    intervals = availability_intervals(availability_df, employee, day)
    if not intervals:
        return None

    break_after  = int(float(settings.get("break_after_hours", 6.0)) * 60)
    break_min_v  = int(settings.get("break_minutes", 45))
    close_from   = int(settings.get("close_from_hour", 1))
    close_to     = int(settings.get("close_to_hour", 4))

    desired_paid_minutes = max(desired_paid_minutes, min_paid_minutes)

    def paid_to_raw(paid: int) -> int:
        if break_min_v > 0 and (paid + break_min_v) >= break_after:
            return paid + break_min_v
        return paid

    raw_dur = paid_to_raw(desired_paid_minutes)
    durations = list(range(raw_dur, paid_to_raw(min_paid_minutes) - 1, -15))
    if not durations:
        durations = [raw_dur]

    for duration in durations:
        for av_start, av_end in intervals:
            if closing:
                preferred_end = 25 * 60  # 01:00 del día siguiente
                end_abs   = preferred_end if av_start <= preferred_end <= av_end else av_end
                start_abs = end_abs - duration
                if start_abs >= av_start and end_abs <= av_end:
                    s = start_abs % (24 * 60)
                    e = end_abs   % (24 * 60)
                    candidate = Shift(s, e, f"{minutes_to_hhmm(s)} - {minutes_to_hhmm(e)}")
                    if shift_closes(candidate, close_from, close_to):
                        return candidate
            else:
                start_abs = av_start
                end_abs   = start_abs + duration
                if end_abs <= av_end:
                    s = start_abs % (24 * 60)
                    e = end_abs   % (24 * 60)
                    return Shift(s, e, f"{minutes_to_hhmm(s)} - {minutes_to_hhmm(e)}")
    return None


def schedule_minutes_worked(
    schedule: Dict[str, Dict[str, Optional[Shift]]],
    employee: str,
    settings: dict,
) -> int:
    ba = float(settings.get("break_after_hours", 6.0))
    bm = int(settings.get("break_minutes", 45))
    return sum(
        shift_paid_minutes(schedule[employee].get(day), ba, bm)
        for day in DAYS
    )


def schedule_off_days(schedule: Dict[str, Dict[str, Optional[Shift]]], employee: str) -> int:
    return sum(1 for day in DAYS if schedule[employee].get(day) is None)


def count_coverage(
    schedule, employees_df: pd.DataFrame, day: str, area: str,
    closing_only: bool, settings: dict
) -> int:
    names  = employees_df[
        (employees_df["Activo"] == True) &
        (employees_df["Area"].map(normalize_area) == normalize_area(area))
    ]["Trabajador"].tolist()
    cff    = int(settings.get("close_from_hour", 1))
    cto    = int(settings.get("close_to_hour", 4))
    count  = 0
    for name in names:
        sh = schedule.get(name, {}).get(day)
        if sh is None:
            continue
        if closing_only and not shift_closes(sh, cff, cto):
            continue
        count += 1
    return count


def count_opening_coverage(
    schedule, employees_df: pd.DataFrame, day: str, area: str
) -> int:
    names = employees_df[
        (employees_df["Activo"] == True) &
        (employees_df["Area"].map(normalize_area) == normalize_area(area))
    ]["Trabajador"].tolist()
    return sum(1 for n in names if is_opening_shift(schedule.get(n, {}).get(day)))


def is_hard_off(requests_df: pd.DataFrame, employee: str, day: str) -> bool:
    if requests_df is None or requests_df.empty:
        return False
    for _, row in requests_df.iterrows():
        if normalize_text(row.get("Trabajador")).upper() != employee.upper():
            continue
        if normalize_day(row.get("Dia")) != day:
            continue
        tipo = normalize_key(row.get("Tipo"))
        if tipo in {
            "NO_TRABAJA", "NO TRABAJA", "NO_TRABAJAR", "NO TRABAJAR",
            "NO DISPONIBLE", "NO_DISPONIBLE", "OFF", "DESCANSO",
            "SOLICITA_DESCANSO", "SOLICITA DESCANSO", "FERIADO", "DIA_LIBRE", "DIA LIBRE",
        }:
            return True
    return False


def get_forced_shift(requests_df: pd.DataFrame, employee: str, day: str) -> Optional[Shift]:
    if requests_df is None or requests_df.empty:
        return None
    for _, row in requests_df.iterrows():
        if normalize_text(row.get("Trabajador")).upper() != employee.upper():
            continue
        if normalize_day(row.get("Dia")) != day:
            continue
        tipo = normalize_key(row.get("Tipo"))
        if tipo in {"TURNO_ESPECIFICO", "TURNO ESPECIFICO", "CAMBIO_TURNO", "CAMBIO TURNO"}:
            start = normalize_text(row.get("Hora Inicio"))
            end   = normalize_text(row.get("Hora Fin"))
            if start and end:
                return make_shift(start, end)
    return None


def choose_shift_for(
    area: str, day: str, closing: bool, pool: dict,
    fallback_start="17:00", fallback_end="23:00"
) -> Shift:
    area    = normalize_area(area)
    options = pool.get((area, day, closing), [])
    if not options and closing:
        options = pool.get((area, day, False), [])
        options = [x for x in options if shift_closes(x)]
    if not options:
        options = pool.get((area, day, False), [])
    if options:
        return options[0]
    return make_shift(fallback_start, "01:00" if closing else fallback_end)


# ─────────────────────────────────────────────────────────────────────────────
# Motor principal de generación
# ─────────────────────────────────────────────────────────────────────────────

def generate_schedule(
    employees_df: pd.DataFrame,
    base_long_df: pd.DataFrame,
    requirements_df: pd.DataFrame,
    availability_df: Optional[pd.DataFrame] = None,
    requests_df: Optional[pd.DataFrame] = None,
    settings: Optional[dict] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """
    Genera el horario semanal a partir del base + solicitudes + disponibilidad.

    Casos manejados:
    1. Solicitud de descanso: libera el día y redistribuye las horas en otro día
       disponible (respetando límites PT/FT exactos en minutos).
    2. Nuevo trabajador: se asigna turnos según su disponibilidad y área para
       alcanzar su objetivo de horas.
    3. Baja (inactivo): no se genera; si era cierre/apertura, se busca sustituto.
    """
    settings = settings or {}
    cff = int(settings.get("close_from_hour", 1))
    cto = int(settings.get("close_to_hour", 4))
    settings = {**settings, "close_from_hour": cff, "close_to_hour": cto}

    ba  = float(settings.get("break_after_hours", 6.0))
    bm  = int(settings.get("break_minutes", 45))
    tol = int(float(settings.get("hour_tolerance", 0.25)) * 60)  # tolerancia en minutos

    warnings: List[str] = []
    notes:    List[str] = []

    employees = employees_df.copy().fillna("")
    employees["Area"]   = employees["Area"].map(normalize_area)
    employees["Turno"]  = employees["Turno"].map(lambda x: normalize_contract(x) or "PT")
    employees["Activo"] = employees["Activo"].fillna(True).astype(bool)

    # Límites en minutos (PT: 1140 min, FT: 2880 min)
    def _max_minutes(turno: str) -> int:
        return PT_MAX_MINUTES if turno == "PT" else FT_MAX_MINUTES

    active = employees[employees["Activo"] == True].copy()

    pool    = build_shift_pool(base_long_df, cff, cto)

    # Mapa base (nombre, día) → turno
    base_map: Dict[Tuple[str, str], Optional[Shift]] = {}
    for _, row in base_long_df.iterrows():
        name = normalize_text(row["Trabajador"])
        day  = normalize_day(row["Dia"])
        base_map[(name, day)] = parse_shift_string(row["Shift"])

    # Inicializar horario con el base (solo activos)
    schedule: Dict[str, Dict[str, Optional[Shift]]] = {
        name: {day: None for day in DAYS}
        for name in active["Trabajador"]
    }
    for name in active["Trabajador"]:
        for day in DAYS:
            schedule[name][day] = base_map.get((name, day))

    locked_off  = set()  # (name, day): día libre por solicitud
    locked_work = set()  # (name, day): turno forzado

    emp_row = {r["Trabajador"]: r for _, r in active.iterrows()}

    def target_minutes(name: str) -> int:
        row   = emp_row[name]
        turno = row["Turno"]
        # Respetar Max Minutos si está definido, sino usar default PT/FT
        max_m = row.get("Max Minutos")
        if max_m and float(max_m) > 0:
            return int(float(max_m))
        return _max_minutes(turno)

    def paid_m(sh: Optional[Shift]) -> int:
        return shift_paid_minutes(sh, ba, bm)

    def sched_minutes(name: str) -> int:
        return schedule_minutes_worked(schedule, name, settings)

    # ── Paso 1: Aplicar solicitudes especiales ──────────────────────────────
    if requests_df is not None and not requests_df.empty:
        req_df = requests_df.fillna("")
        for _, req in req_df.iterrows():
            name = normalize_text(req.get("Trabajador"))
            day  = normalize_day(req.get("Dia"))
            tipo = normalize_key(req.get("Tipo"))

            if name not in schedule or day not in DAYS:
                continue

            if is_hard_off(req_df, name, day):
                old = schedule[name].get(day)
                schedule[name][day] = None
                locked_off.add((name, day))

                was_closer  = old is not None and shift_closes(old, cff, cto)
                was_opener  = old is not None and is_opening_shift(old)

                role_str = ""
                if was_closer:
                    role_str = " (era cierre)"
                elif was_opener:
                    role_str = " (era apertura)"

                notes.append(
                    f"✅ Solicitud aprobada: {name} libre el {day}{role_str}. "
                    f"Se intentará reubicar sus horas otro día."
                )
                if was_closer:
                    notes.append(
                        f"⚠️ {name} cubría cierre en {day}; buscando sustituto de área {emp_row[name]['Area']}."
                    )

            forced = get_forced_shift(req_df, name, day)
            if forced:
                schedule[name][day] = forced
                locked_work.add((name, day))

    # ── Funciones auxiliares ────────────────────────────────────────────────

    def req_for(day: str, area: str) -> Tuple[int, int]:
        if requirements_df is None or requirements_df.empty:
            return 0, 0
        rows = requirements_df[
            (requirements_df["Dia"].map(normalize_day) == day) &
            (requirements_df["Area"].map(normalize_area) == normalize_area(area))
        ]
        if rows.empty:
            return 0, 0
        r = rows.iloc[0]
        return int(r.get("Min Personas", 0) or 0), int(r.get("Min Cierres", 0) or 0)

    def removal_breaks_req(name: str, day: str) -> bool:
        area = emp_row[name]["Area"]
        sh   = schedule[name].get(day)
        if sh is None:
            return False
        min_total, min_closers = req_for(day, area)
        current_total   = count_coverage(schedule, active, day, area, False, settings)
        current_closers = count_coverage(schedule, active, day, area, True, settings)
        if current_total - 1 < min_total:
            return True
        if shift_closes(sh, cff, cto) and current_closers - 1 < min_closers:
            return True
        return False

    def can_place(
        name: str, day: str, shift: Shift,
        allow_replace_nonclosing: bool = False,
    ) -> bool:
        """
        Verifica si se puede colocar `shift` al trabajador `name` en `day`.

        Límite de horas: DURO. new_tot no puede superar target_minutes(name)
        bajo ninguna circunstancia (ni un minuto más).
        """
        if (name, day) in locked_off:
            return False
        current = schedule[name].get(day)
        if current is not None:
            if not allow_replace_nonclosing:
                return False
            if (name, day) in locked_work:
                return False
            if shift_closes(current, cff, cto):
                return False
        off_days = schedule_off_days(schedule, name)
        # Si vamos a ocupar un día libre, verificar mínimos de descanso
        if current is None and off_days - 1 < int(emp_row[name]["Min Descansos"]):
            return False
        old_m   = paid_m(current) if current else 0
        new_tot = sched_minutes(name) - old_m + paid_m(shift)
        # Límite DURO: ni un minuto sobre el máximo
        if new_tot > target_minutes(name):
            return False
        if not shift_fits_availability(shift, name, day, availability_df):
            return False
        return True

    def candidate_shifts_for(
        name: str, day: str, closing: bool,
        desired_minutes: Optional[int] = None,
    ) -> List[Shift]:
        area    = emp_row[name]["Area"]
        options: List[Shift] = []
        pool_cands = pool.get((area, day, closing), []) + (
            [] if closing else pool.get((area, day, True), [])
        )
        seen_raw = set()
        for sh in pool_cands:
            if sh and sh.raw not in seen_raw:
                seen_raw.add(sh.raw)
                options.append(sh)
        if desired_minutes is None:
            desired_minutes = max(target_minutes(name) - sched_minutes(name), 120)
        dyn = dynamic_shift_from_availability(
            name, day, availability_df, desired_minutes, settings, closing=closing
        )
        if dyn and dyn.raw not in seen_raw:
            options.insert(0, dyn)
        # Filtrar por tipo de cierre requerido
        filtered = []
        for sh in options:
            is_c = shift_closes(sh, cff, cto)
            if closing and not is_c:
                continue
            filtered.append(sh)
        return filtered

    def trim_to_target(name: str) -> bool:
        """
        Elimina turnos no críticos hasta que:
          - Los minutos pagados estén dentro del límite máximo DURO (sin tolerancia).
          - Se cumplan los días mínimos de descanso.

        El límite es ESTRICTO: ni un minuto sobre target_minutes(name).
        La tolerancia (tol) solo se usa en la dirección de "faltan horas",
        nunca para permitir exceder el techo.
        """
        changed = False
        guard   = 0
        while guard < 30:
            guard += 1
            mins = sched_minutes(name)
            tgt  = target_minutes(name)
            # Condición de parada: dentro del techo Y descansos OK
            over_cap   = mins > tgt          # excede límite máximo (duro, sin tol)
            under_rest = schedule_off_days(schedule, name) < int(emp_row[name]["Min Descansos"])
            if not over_cap and not under_rest:
                return changed
            candidates = []
            for day in DAYS:
                sh = schedule[name].get(day)
                if sh is None or (name, day) in locked_work:
                    continue
                critical = 1 if removal_breaks_req(name, day) else 0
                closes   = 1 if shift_closes(sh, cff, cto) else 0
                # Priorizar quitar el turno que deja el total más cerca de tgt
                after_removal = mins - paid_m(sh)
                excess_after  = abs(after_removal - tgt)
                candidates.append((critical, closes, excess_after, -paid_m(sh), day))
            safe = [c for c in candidates if c[0] == 0]
            candidates = safe or candidates
            if not candidates:
                break
            candidates.sort()
            schedule[name][candidates[0][-1]] = None
            changed = True
        return changed

    def assign_to_cover(day: str, area: str, closing: bool) -> bool:
        current_count = count_coverage(schedule, active, day, area, closing, settings)
        _, min_closers = req_for(day, area)
        min_needed = min_closers if closing else req_for(day, area)[0]
        if current_count >= min_needed:
            return True
        candidates = []
        for name, row in emp_row.items():
            if row["Area"] != area:
                continue
            current_shift = schedule[name].get(day)
            replace_bonus = 0
            if current_shift is not None:
                if not closing or shift_closes(current_shift, cff, cto):
                    continue
                replace_bonus = -2
            for sh in candidate_shifts_for(name, day, closing=closing):
                old_m   = paid_m(current_shift) if current_shift else 0
                add_m   = paid_m(sh) - old_m
                new_tot = sched_minutes(name) - old_m + paid_m(sh)
                if new_tot > target_minutes(name):   # techo DURO
                    continue
                if can_place(name, day, sh, allow_replace_nonclosing=True):
                    base_sh = base_map.get((name, day))
                    base_cb = -1 if base_sh and shift_closes(base_sh, cff, cto) else 0
                    under   = max(target_minutes(name) - sched_minutes(name), 0)
                    candidates.append((replace_bonus + base_cb, -under, sched_minutes(name), name, sh))
                    break
        if not candidates:
            missing = min_needed - current_count
            warnings.append(
                f"⚠️ Cobertura insuficiente {'cierre' if closing else 'dotación'}: "
                f"{day} / {area}. Faltan {missing} persona(s)."
            )
            return False
        candidates.sort()
        _, _, _, name, shift = candidates[0]
        schedule[name][day] = shift
        return True

    # ── Paso 2: Reducir excesos antes de reparar ────────────────────────────
    for name in list(schedule.keys()):
        trim_to_target(name)

    areas = sorted(active["Area"].dropna().unique().tolist())

    # ── Paso 3: Reparar coberturas de cierre ───────────────────────────────
    # (incluye casos donde el que cerraba pidió descanso o renunció)
    for day in DAYS:
        for area in areas:
            _, min_closers = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, True, settings) < min_closers and guard < 20:
                guard += 1
                if not assign_to_cover(day, area, closing=True):
                    break

    # ── Paso 4: Reparar cobertura general ──────────────────────────────────
    for day in DAYS:
        for area in areas:
            min_total, _ = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, False, settings) < min_total and guard < 20:
                guard += 1
                if not assign_to_cover(day, area, closing=False):
                    break

    # ── Paso 5: Completar horas objetivo (incluye nuevos trabajadores) ──────
    for name, row in emp_row.items():
        guard = 0
        while sched_minutes(name) < target_minutes(name) - tol and guard < 30:
            guard += 1
            remaining = target_minutes(name) - sched_minutes(name)

            possible_days = []
            for day in DAYS:
                if (name, day) in locked_off:
                    continue
                if schedule[name].get(day) is not None:
                    continue
                if schedule_off_days(schedule, name) - 1 < int(row["Min Descansos"]):
                    continue
                if not availability_intervals(availability_df, name, day):
                    continue
                base_bonus = 0 if base_map.get((name, day)) else 1
                area_cov   = count_coverage(schedule, active, day, row["Area"], False, settings)
                possible_days.append((base_bonus, area_cov, day))

            possible_days.sort()
            assigned = False

            for _, _, day in possible_days:
                _, min_closers = req_for(day, row["Area"])
                closing_needed = count_coverage(schedule, active, day, row["Area"], True, settings) < min_closers
                options = candidate_shifts_for(name, day, closing=closing_needed, desired_minutes=remaining)
                if not options and not closing_needed:
                    options = candidate_shifts_for(name, day, closing=True, desired_minutes=remaining)
                if not options:
                    dyn = dynamic_shift_from_availability(name, day, availability_df, remaining, settings, closing=False)
                    options = [dyn] if dyn else []

                for sh in options:
                    if sh is None:
                        continue
                    add_m = paid_m(sh)
                    if add_m <= 0:
                        continue
                    # Si este turno se pasa del techo, intentar uno más corto a medida
                    if sched_minutes(name) + add_m > target_minutes(name):
                        custom = dynamic_shift_from_availability(
                            name, day, availability_df, remaining, settings,
                            closing=shift_closes(sh, cff, cto), min_paid_minutes=60,
                        )
                        if custom and sched_minutes(name) + paid_m(custom) <= target_minutes(name):
                            sh = custom
                        else:
                            continue
                    if can_place(name, day, sh):
                        schedule[name][day] = sh
                        assigned = True
                        break
                if assigned:
                    break

            if not assigned:
                warnings.append(
                    f"⚠️ {name} queda con {format_paid_hours(sched_minutes(name))} h "
                    f"de {format_paid_hours(target_minutes(name))} h objetivo. "
                    f"No hay disponibilidad suficiente."
                )
                break

    # ── Paso 6: Recorte final + último pase de cobertura ───────────────────
    for name in list(schedule.keys()):
        trim_to_target(name)

    for day in DAYS:
        for area in areas:
            _, min_closers = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, True, settings) < min_closers and guard < 10:
                guard += 1
                if not assign_to_cover(day, area, closing=True):
                    break
            min_total, _ = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, False, settings) < min_total and guard < 10:
                guard += 1
                if not assign_to_cover(day, area, closing=False):
                    break

    # ── Construir DataFrames de salida ──────────────────────────────────────
    long_rows = []
    for _, row in active.iterrows():
        name, area, turno = row["Trabajador"], row["Area"], row["Turno"]
        for day in DAYS:
            sh = schedule[name].get(day)
            pm = shift_paid_minutes(sh, ba, bm)
            long_rows.append({
                "Trabajador":    name,
                "Area":          area,
                "Turno":         turno,
                "Dia":           day,
                "Shift":         format_shift(sh),
                "Hora Inicio":   minutes_to_hhmm(sh.start_min) if sh else "",
                "Hora Fin":      minutes_to_hhmm(sh.end_min)   if sh else "",
                "Minutos Brutos": sh.duration_minutes if sh else 0,
                "Minutos Pagados": pm,
                "Horas Pagadas": format_paid_hours(pm),   # HH:MM sin decimales
                "Cierre":        shift_closes(sh, cff, cto),
                "Apertura":      is_opening_shift(sh),
            })

    out_long = pd.DataFrame(long_rows)
    out_wide = long_to_wide(out_long)
    summary  = build_summary(out_long, active)

    # Validaciones finales
    for _, row in summary.iterrows():
        tgt = target_minutes(row["Trabajador"])
        gen = int(row["Minutos Generados"])
        if gen > tgt:                # techo DURO: cualquier minuto extra es error
            warnings.append(
                f"🔴 {row['Trabajador']} SUPERA límite: "
                f"{format_paid_hours(gen)} > {format_paid_hours(tgt)}."
            )
        elif gen < tgt - tol:        # déficit con tolerancia
            warnings.append(
                f"🟡 {row['Trabajador']} no completa objetivo: "
                f"{format_paid_hours(gen)} / {format_paid_hours(tgt)}."
            )
        if int(row["Descansos"]) < int(row["Min Descansos"]):
            warnings.append(
                f"🔴 {row['Trabajador']} no cumple descansos mínimos: "
                f"{row['Descansos']} < {row['Min Descansos']}."
            )

    if requirements_df is not None and not requirements_df.empty:
        for _, req in requirements_df.iterrows():
            day        = normalize_day(req["Dia"])
            area       = normalize_area(req["Area"])
            min_total  = int(req.get("Min Personas", 0) or 0)
            min_close  = int(req.get("Min Cierres",  0) or 0)
            total      = count_coverage(schedule, active, day, area, False, settings)
            close_cnt  = count_coverage(schedule, active, day, area, True,  settings)
            if total < min_total:
                warnings.append(f"🔴 Dotación insuficiente {day}/{area}: {total}/{min_total}.")
            if close_cnt < min_close:
                warnings.append(f"🔴 Cierres insuficientes {day}/{area}: {close_cnt}/{min_close}.")

    seen      = set()
    all_msgs  = notes + warnings
    deduped   = [w for w in all_msgs if not (w in seen or seen.add(w))]

    return out_wide, out_long, summary, deduped


def build_summary(long_df: pd.DataFrame, employees_df: pd.DataFrame) -> pd.DataFrame:
    workdays = long_df[long_df["Shift"] != "OFF"].groupby("Trabajador")["Dia"].nunique().to_dict()
    paid_min = long_df.groupby("Trabajador")["Minutos Pagados"].sum().to_dict() if "Minutos Pagados" in long_df.columns else {}
    closes   = long_df.groupby("Trabajador")["Cierre"].sum().to_dict() if "Cierre" in long_df.columns else {}
    openings = long_df.groupby("Trabajador")["Apertura"].sum().to_dict() if "Apertura" in long_df.columns else {}

    result = employees_df[["Trabajador", "Area", "Turno", "Max Horas", "Min Descansos"]].copy()
    result["Minutos Generados"] = result["Trabajador"].map(paid_min).fillna(0).astype(int)
    result["Horas Generadas"]   = result["Trabajador"].map(
        lambda n: format_paid_hours(int(paid_min.get(n, 0)))
    )
    result["Dias Trabajados"] = result["Trabajador"].map(workdays).fillna(0).astype(int)
    result["Descansos"]       = 7 - result["Dias Trabajados"]
    result["Cierres"]         = result["Trabajador"].map(closes).fillna(0).astype(int)
    result["Aperturas"]       = result["Trabajador"].map(openings).fillna(0).astype(int)
    return result


def export_schedule_excel(
    wide_df: pd.DataFrame,
    long_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    warnings: List[str],
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        wide_df.to_excel(writer,    sheet_name="Horario Semanal", index=False)
        long_df.to_excel(writer,    sheet_name="Formato Largo",   index=False)
        summary_df.to_excel(writer, sheet_name="Resumen",         index=False)
        pd.DataFrame({"Validaciones": warnings or ["Sin advertencias"]}).to_excel(
            writer, sheet_name="Validaciones", index=False
        )
        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                max_len = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in col
                )
                ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 12), 36)
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
    return output.getvalue()
