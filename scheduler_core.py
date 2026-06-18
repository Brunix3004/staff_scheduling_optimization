from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

DAYS = ["LUNES", "MARTES", "MIERCOLES", "JUEVES", "VIERNES", "SABADO", "DOMINGO"]
DAY_ALIASES = {
    "LUNES": "LUNES", "LU": "LUNES", "L": "LUNES",
    "MARTES": "MARTES", "MA": "MARTES", "M": "MARTES",
    "MIERCOLES": "MIERCOLES", "MIÉRCOLES": "MIERCOLES", "MI": "MIERCOLES", "X": "MIERCOLES",
    "JUEVES": "JUEVES", "JU": "JUEVES", "J": "JUEVES",
    "VIERNES": "VIERNES", "VI": "VIERNES", "V": "VIERNES",
    "SABADO": "SABADO", "SÁBADO": "SABADO", "SA": "SABADO", "S": "SABADO",
    "DOMINGO": "DOMINGO", "DO": "DOMINGO", "D": "DOMINGO",
}
AREA_ALIASES = {
    "SERVICIO": "SERVICIO", "SVC": "SERVICIO", "SERVICE": "SERVICIO",
    "PRODUCCION": "PRODUCCION", "PRODUCCIÓN": "PRODUCCION", "PROD": "PRODUCCION", "COCINA": "PRODUCCION",
}
CONTRACT_ALIASES = {
    "PT": "PT", "PART TIME": "PT", "PART-TIME": "PT", "PARTTIME": "PT",
    "FT": "FT", "FULL TIME": "FT", "FULL-TIME": "FT", "FULLTIME": "FT",
}
OFF_TOKENS = {"", "OFF", "NULL", "NULO", "LIBRE", "DESCANSO", "DESC", "NA", "N/A", "-"}


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


def normalize_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    txt = str(value).strip()
    # Normalize common typo found in the sample: NULLNUL, NULLNULL, etc.
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
    # Handles values like NULLNUL / NULL NULL / NULNUL.
    return bool(txt) and txt.replace(" ", "") in {"NULLNUL", "NULLNULL", "NULNUL"}


def parse_hhmm(value: str) -> int:
    txt = normalize_text(value)
    match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", txt)
    if not match:
        raise ValueError(f"Hora inválida: {value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Hora fuera de rango: {value!r}")
    return hour * 60 + minute


def minutes_to_hhmm(minutes: int) -> str:
    minutes = minutes % (24 * 60)
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
    left, right = [p.strip() for p in txt.split("-", 1)]
    try:
        start = parse_hhmm(left)
        end = parse_hhmm(right)
    except ValueError:
        return None
    return Shift(start, end, f"{minutes_to_hhmm(start)} - {minutes_to_hhmm(end)}")


def shift_paid_hours(shift: Optional[Shift], break_after_hours: float = 6.0, break_minutes: int = 45) -> float:
    if shift is None:
        return 0.0
    hours = shift.raw_hours
    if break_minutes > 0 and hours >= break_after_hours:
        hours -= break_minutes / 60
    return max(hours, 0)


def shift_closes(shift: Optional[Shift], close_from_hour: int = 1, close_to_hour: int = 4) -> bool:
    if shift is None:
        return False
    # Bembos rule: a closing shift is one that crosses midnight and ends at/after 01:00.
    # Example: 16:15 - 01:00 = closing; 15:00 - 23:00 is not closing.
    if shift.end_min > shift.start_min:
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
    """Reads a schedule sheet, preferring horarios/horario_base if available."""
    xls = pd.ExcelFile(file_or_path)
    lower_map = {str(s).lower().strip(): s for s in xls.sheet_names}
    if "horarios" in lower_map:
        sheet = lower_map["horarios"]
    elif "horario_base" in lower_map:
        sheet = lower_map["horario_base"]
    else:
        sheet = xls.sheet_names[0]
    # Preserve strings; avoid automatic time/date conversions.
    return pd.read_excel(file_or_path, sheet_name=sheet, dtype=object)


def normalize_input_excel(file_or_path, settings: Optional[dict] = None) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns employees_df, schedule_long_df, wide_df.

    Supports:
    1) Wide format: COLABORADOR/AREA/TURNO/LUNES...DOMINGO
    2) Long format: Trabajador/Area/Dia/Hora Inicio/Hora Fin
    """
    settings = settings or {}
    break_after_hours = float(settings.get("break_after_hours", 6.0))
    break_minutes = int(settings.get("break_minutes", 45))
    ft_threshold = float(settings.get("ft_infer_threshold", 35.0))

    raw_df = read_excel_any(file_or_path)
    raw_df = raw_df.dropna(how="all")
    raw_df.columns = [normalize_text(c) for c in raw_df.columns]

    worker_col = _find_column(raw_df, ["COLABORADOR", "TRABAJADOR", "NOMBRE", "EMPLEADO"])
    area_col = _find_column(raw_df, ["AREA", "ÁREA"])
    turno_col = _find_column(raw_df, ["TURNO", "TIPO", "CONTRATO", "PT/FT"])
    day_cols = {day: _find_column(raw_df, [day]) for day in DAYS}

    # Long format detection
    dia_col = _find_column(raw_df, ["DIA", "DÍA"])
    inicio_col = _find_column(raw_df, ["HORA INICIO", "INICIO", "ENTRADA", "HORA ENTRADA"])
    fin_col = _find_column(raw_df, ["HORA FIN", "FIN", "SALIDA", "HORA SALIDA"])

    if worker_col is None or area_col is None:
        raise ValueError("El Excel debe tener columnas de colaborador/trabajador y área.")

    rows: List[dict] = []
    if dia_col and inicio_col and fin_col:
        for _, row in raw_df.iterrows():
            name = normalize_text(row.get(worker_col))
            if not name:
                continue
            area = normalize_area(row.get(area_col))
            contract = normalize_contract(row.get(turno_col)) if turno_col else ""
            day = normalize_day(row.get(dia_col))
            start_raw = normalize_text(row.get(inicio_col))
            end_raw = normalize_text(row.get(fin_col))
            if day not in DAYS:
                continue
            shift = None
            if start_raw and end_raw and not is_off_cell(start_raw) and not is_off_cell(end_raw):
                shift = parse_shift_cell(f"{start_raw} - {end_raw}")
            rows.append({
                "Trabajador": name,
                "Area": area,
                "Turno": contract,
                "Dia": day,
                "Shift": format_shift(shift),
                "Hora Inicio": minutes_to_hhmm(shift.start_min) if shift else "",
                "Hora Fin": minutes_to_hhmm(shift.end_min) if shift else "",
                "Raw Hours": round(shift.raw_hours if shift else 0, 2),
                "Horas Pagadas": round(shift_paid_hours(shift, break_after_hours, break_minutes), 2),
                "Cierre": shift_closes(shift),
            })
    else:
        if not all(day_cols.values()):
            missing = [d for d, c in day_cols.items() if c is None]
            raise ValueError(f"Faltan columnas de día: {', '.join(missing)}")
        for _, row in raw_df.iterrows():
            name = normalize_text(row.get(worker_col))
            if not name:
                continue
            area = normalize_area(row.get(area_col))
            contract = normalize_contract(row.get(turno_col)) if turno_col else ""
            for day, col in day_cols.items():
                shift = parse_shift_cell(row.get(col))
                rows.append({
                    "Trabajador": name,
                    "Area": area,
                    "Turno": contract,
                    "Dia": day,
                    "Shift": format_shift(shift),
                    "Hora Inicio": minutes_to_hhmm(shift.start_min) if shift else "",
                    "Hora Fin": minutes_to_hhmm(shift.end_min) if shift else "",
                    "Raw Hours": round(shift.raw_hours if shift else 0, 2),
                    "Horas Pagadas": round(shift_paid_hours(shift, break_after_hours, break_minutes), 2),
                    "Cierre": shift_closes(shift),
                })

    long_df = pd.DataFrame(rows)
    if long_df.empty:
        raise ValueError("No se detectaron turnos válidos en el Excel.")

    # Infer employees
    employee_base = (
        long_df.groupby(["Trabajador", "Area"], as_index=False)
        .agg({"Horas Pagadas": "sum", "Dia": "count", "Turno": lambda s: next((x for x in s if normalize_contract(x)), "")})
    )
    worked_days = long_df[long_df["Shift"] != "OFF"].groupby("Trabajador")["Dia"].nunique().to_dict()
    employee_base["Horas Semana Base"] = employee_base["Horas Pagadas"].round(2)
    employee_base["Dias Trabajados Base"] = employee_base["Trabajador"].map(worked_days).fillna(0).astype(int)
    employee_base["Descansos Base"] = 7 - employee_base["Dias Trabajados Base"]
    employee_base["Turno"] = employee_base.apply(
        lambda r: normalize_contract(r["Turno"]) or ("FT" if r["Horas Semana Base"] >= ft_threshold else "PT"), axis=1
    )
    employee_base["Activo"] = True
    employee_base["Max Horas"] = employee_base["Turno"].map({"PT": 19.0, "FT": 48.0}).fillna(19.0)
    employee_base["Min Descansos"] = employee_base["Turno"].map({"PT": 2, "FT": 1}).fillna(2).astype(int)
    employees_df = employee_base[["Trabajador", "Area", "Turno", "Activo", "Max Horas", "Min Descansos", "Horas Semana Base", "Dias Trabajados Base", "Descansos Base"]]

    wide_df = long_to_wide(long_df)
    return employees_df, long_df, wide_df


def long_to_wide(long_df: pd.DataFrame) -> pd.DataFrame:
    base = long_df[["Trabajador", "Area", "Turno"]].drop_duplicates("Trabajador")
    pivot = long_df.pivot_table(index="Trabajador", columns="Dia", values="Shift", aggfunc="first")
    pivot = pivot.reindex(columns=DAYS)
    wide = base.merge(pivot.reset_index(), on="Trabajador", how="left")
    for day in DAYS:
        wide[day] = wide[day].fillna("OFF")
    return wide[["Trabajador", "Area", "Turno"] + DAYS]


def parse_shift_string(value: str) -> Optional[Shift]:
    return parse_shift_cell(value)


def build_requirements_from_base(long_df: pd.DataFrame) -> pd.DataFrame:
    work = long_df[long_df["Shift"] != "OFF"].copy()
    if work.empty:
        return pd.DataFrame(columns=["Dia", "Area", "Min Personas", "Min Cierres"])
    # The requirement table is focused on closing coverage. Only shifts that cross midnight
    # and end at/after 01:00 count as closings.
    work["Cierre"] = work["Shift"].map(lambda s: shift_closes(parse_shift_string(s)))
    closers = work[work["Cierre"]].copy()
    rows = []
    for day in DAYS:
        for area in ["SERVICIO", "PRODUCCION"]:
            count = int(closers[(closers["Dia"] == day) & (closers["Area"].map(normalize_area) == area)]["Trabajador"].nunique())
            rows.append({"Dia": day, "Area": area, "Min Personas": count, "Min Cierres": count})
    grouped = pd.DataFrame(rows)
    grouped["Dia"] = pd.Categorical(grouped["Dia"], categories=DAYS, ordered=True)
    grouped = grouped.sort_values(["Dia", "Area"]).reset_index(drop=True)
    return grouped


def build_availability_from_base(long_df: pd.DataFrame) -> pd.DataFrame:
    av = long_df[long_df["Shift"] != "OFF"][["Trabajador", "Dia", "Hora Inicio", "Hora Fin"]].copy()
    av["Disponible"] = True
    return av.reset_index(drop=True)


def build_shift_pool(long_df: pd.DataFrame) -> Dict[Tuple[str, str, bool], List[Shift]]:
    pool: Dict[Tuple[str, str, bool], List[Shift]] = {}
    work = long_df[long_df["Shift"] != "OFF"].copy()
    for (area, day, cierre, shift_str), grp in work.groupby(["Area", "Dia", "Cierre", "Shift"]):
        shift = parse_shift_string(shift_str)
        if shift:
            key = (normalize_area(area), day, bool(cierre))
            pool.setdefault(key, [])
            # Repeat by frequency so the most common shift naturally comes first after sorting.
            pool[key].extend([shift] * len(grp))
    for key in list(pool):
        freq = {}
        for sh in pool[key]:
            freq[sh.raw] = freq.get(sh.raw, 0) + 1
        unique = [parse_shift_string(k) for k, _ in sorted(freq.items(), key=lambda x: -x[1])]
        pool[key] = [x for x in unique if x]
    return pool


def make_shift(start: str, end: str) -> Shift:
    return Shift(parse_hhmm(start), parse_hhmm(end), f"{minutes_to_hhmm(parse_hhmm(start))} - {minutes_to_hhmm(parse_hhmm(end))}")



def availability_intervals(availability_df: pd.DataFrame, employee: str, day: str) -> List[Tuple[int, int]]:
    """Return availability intervals as absolute minutes in a 0..2880 timeline.

    A row 16:00-01:00 becomes (960, 1500). Multiple rows per day are allowed.
    """
    if availability_df is None or availability_df.empty:
        return [(0, 24 * 60)]
    df = availability_df.copy().fillna("")
    rows = df[
        (df["Trabajador"].astype(str).str.upper() == employee.upper())
        & (df["Dia"].astype(str).map(normalize_day) == day)
    ]
    if rows.empty:
        return []
    intervals: List[Tuple[int, int]] = []
    for _, row in rows.iterrows():
        available_raw = row.get("Disponible", True)
        available = bool(available_raw)
        if normalize_key(available_raw) in {"FALSE", "0", "NO", "N", "OFF", "NO DISPONIBLE"}:
            available = False
        if not available:
            continue
        start = normalize_text(row.get("Hora Inicio"))
        end = normalize_text(row.get("Hora Fin"))
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


def paid_hours_to_raw_minutes(paid_hours: float, settings: dict) -> int:
    paid_hours = max(float(paid_hours), 0.0)
    break_after = float(settings.get("break_after_hours", 6.0))
    break_minutes = int(settings.get("break_minutes", 45))
    raw = paid_hours * 60
    # If a shift reaches the break threshold after adding break time, use raw = paid + break.
    if break_minutes > 0 and (raw + break_minutes) >= break_after * 60:
        raw += break_minutes
    return max(int(round(raw)), 15)


def shift_from_absolute(start_abs: int, end_abs: int) -> Shift:
    start = start_abs % (24 * 60)
    end = end_abs % (24 * 60)
    return Shift(start, end, f"{minutes_to_hhmm(start)} - {minutes_to_hhmm(end)}")


def dynamic_shift_from_availability(
    employee: str,
    day: str,
    availability_df: pd.DataFrame,
    desired_paid_hours: float,
    settings: dict,
    closing: bool = False,
    min_paid_hours: float = 2.0,
) -> Optional[Shift]:
    """Create a shift inside availability to help complete weekly hours.

    For closing coverage it tries to end at 01:00. Otherwise it starts at the
    beginning of the first available block. The shift duration is based on the
    paid-hours target, adding refrigerio when needed.
    """
    intervals = availability_intervals(availability_df, employee, day)
    if not intervals:
        return None
    desired_paid_hours = max(float(desired_paid_hours), min_paid_hours)
    raw_minutes = paid_hours_to_raw_minutes(desired_paid_hours, settings)
    # Try exact or slightly smaller durations in 15-minute steps.
    duration_options = list(range(raw_minutes, paid_hours_to_raw_minutes(min_paid_hours, settings) - 1, -15))
    if not duration_options:
        duration_options = [raw_minutes]

    for duration in duration_options:
        for av_start, av_end in intervals:
            if closing:
                preferred_end = 25 * 60  # 01:00 next day
                end_abs = preferred_end if av_start <= preferred_end <= av_end else av_end
                start_abs = end_abs - duration
                candidate = shift_from_absolute(start_abs, end_abs)
                if start_abs >= av_start and end_abs <= av_end and shift_closes(candidate, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)):
                    return candidate
            else:
                start_abs = av_start
                end_abs = start_abs + duration
                candidate = shift_from_absolute(start_abs, end_abs)
                if end_abs <= av_end:
                    return candidate
    return None


def schedule_hours(schedule: Dict[str, Dict[str, Optional[Shift]]], employee: str, settings: dict) -> float:
    return round(sum(shift_paid_hours(schedule[employee].get(day), settings.get("break_after_hours", 6.0), settings.get("break_minutes", 45)) for day in DAYS), 2)


def schedule_off_days(schedule: Dict[str, Dict[str, Optional[Shift]]], employee: str) -> int:
    return sum(1 for day in DAYS if schedule[employee].get(day) is None)


def count_coverage(schedule, employees_df: pd.DataFrame, day: str, area: str, closing_only: bool, settings: dict) -> int:
    names = employees_df[(employees_df["Activo"] == True) & (employees_df["Area"].map(normalize_area) == normalize_area(area))]["Trabajador"].tolist()
    count = 0
    for name in names:
        sh = schedule.get(name, {}).get(day)
        if sh is None:
            continue
        if closing_only and not shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)):
            continue
        count += 1
    return count


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
            end = normalize_text(row.get("Hora Fin"))
            if start and end:
                return make_shift(start, end)
    return None


def choose_shift_for(area: str, day: str, closing: bool, pool: dict, fallback_start="17:00", fallback_end="23:00") -> Shift:
    area = normalize_area(area)
    options = pool.get((area, day, closing), [])
    if not options and closing:
        options = pool.get((area, day, False), [])
        options = [x for x in options if shift_closes(x)]
    if not options:
        options = pool.get((area, day, False), [])
    if options:
        return options[0]
    return make_shift(fallback_start, "01:00" if closing else fallback_end)


def generate_schedule(
    employees_df: pd.DataFrame,
    base_long_df: pd.DataFrame,
    requirements_df: pd.DataFrame,
    availability_df: Optional[pd.DataFrame] = None,
    requests_df: Optional[pd.DataFrame] = None,
    settings: Optional[dict] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """Generate a weekly schedule by repairing the previous/base schedule.

    Core behavior:
    - A special request does not simply delete a shift; it triggers a repair.
    - PT/FT employees are pushed toward their weekly target hours.
    - If a closer asks for rest, another person from the same area is assigned
      to closing coverage when possible.
    - Availability is treated as a hard constraint.
    """
    settings = settings or {}
    settings = {**settings, "close_from_hour": int(settings.get("close_from_hour", 1)), "close_to_hour": int(settings.get("close_to_hour", 4))}
    warnings: List[str] = []
    info: List[str] = []

    employees = employees_df.copy().fillna("")
    employees["Area"] = employees["Area"].map(normalize_area)
    employees["Turno"] = employees["Turno"].map(lambda x: normalize_contract(x) or "PT")
    employees["Activo"] = employees["Activo"].fillna(True).astype(bool)
    default_hours = employees["Turno"].map({"PT": settings.get("pt_hours", 19.0), "FT": settings.get("ft_hours", 48.0)}).fillna(settings.get("pt_hours", 19.0))
    employees["Max Horas"] = pd.to_numeric(employees.get("Max Horas", default_hours), errors="coerce").fillna(default_hours).astype(float)
    employees["Min Descansos"] = pd.to_numeric(employees.get("Min Descansos", employees["Turno"].map({"PT": 2, "FT": 1})), errors="coerce").fillna(employees["Turno"].map({"PT": 2, "FT": 1})).astype(int)

    active = employees[employees["Activo"] == True].copy()
    pool = build_shift_pool(base_long_df)
    tol = float(settings.get("hour_tolerance", 0.25))

    schedule: Dict[str, Dict[str, Optional[Shift]]] = {name: {day: None for day in DAYS} for name in active["Trabajador"]}
    base_map: Dict[Tuple[str, str], Optional[Shift]] = {}
    for _, row in base_long_df.iterrows():
        name = normalize_text(row["Trabajador"])
        day = normalize_day(row["Dia"])
        base_map[(name, day)] = parse_shift_string(row["Shift"])

    locked_off = set()
    locked_work = set()
    request_notes: List[str] = []

    for name in active["Trabajador"]:
        for day in DAYS:
            schedule[name][day] = base_map.get((name, day))

    if requests_df is not None and not requests_df.empty:
        requests_df = requests_df.fillna("")
        for _, req in requests_df.iterrows():
            name = normalize_text(req.get("Trabajador"))
            day = normalize_day(req.get("Dia"))
            tipo = normalize_key(req.get("Tipo"))
            if name not in schedule or day not in DAYS:
                continue
            if is_hard_off(requests_df, name, day):
                old = schedule[name].get(day)
                schedule[name][day] = None
                locked_off.add((name, day))
                request_notes.append(f"Solicitud aplicada: {name} no trabaja {day}. El sistema intentará reubicar sus horas.")
                if old and shift_closes(old, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)):
                    request_notes.append(f"{name} cubría cierre en {day}; se buscará reemplazo de su misma área.")
            forced = get_forced_shift(requests_df, name, day)
            if forced:
                schedule[name][day] = forced
                locked_work.add((name, day))

    emp_row = {r["Trabajador"]: r for _, r in active.iterrows()}

    def target_hours(name: str) -> float:
        return float(emp_row[name]["Max Horas"])

    def paid(sh: Optional[Shift]) -> float:
        return shift_paid_hours(sh, settings.get("break_after_hours", 6.0), settings.get("break_minutes", 45))

    def req_for(day: str, area: str) -> Tuple[int, int]:
        if requirements_df is None or requirements_df.empty:
            return 0, 0
        req_df = requirements_df.copy().fillna(0)
        rows = req_df[(req_df["Dia"].map(normalize_day) == day) & (req_df["Area"].map(normalize_area) == normalize_area(area))]
        if rows.empty:
            return 0, 0
        row = rows.iloc[0]
        return int(row.get("Min Personas", 0) or 0), int(row.get("Min Cierres", 0) or 0)

    def removal_breaks_req(name: str, day: str) -> bool:
        area = emp_row[name]["Area"]
        sh = schedule[name].get(day)
        if sh is None:
            return False
        min_total, min_closers = req_for(day, area)
        current_total = count_coverage(schedule, active, day, area, False, settings)
        current_closers = count_coverage(schedule, active, day, area, True, settings)
        if current_total - 1 < min_total:
            return True
        if shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)) and current_closers - 1 < min_closers:
            return True
        return False

    def can_place(name: str, day: str, shift: Shift, allow_replace_nonclosing: bool = False, allow_over_target: float = 0.0) -> bool:
        if (name, day) in locked_off:
            return False
        current = schedule[name].get(day)
        if current is not None:
            if not allow_replace_nonclosing:
                return False
            if (name, day) in locked_work:
                return False
            if shift_closes(current, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)):
                return False
        if schedule_off_days(schedule, name) - (1 if current is None else 0) < int(emp_row[name]["Min Descansos"]):
            return False
        old_hours = paid(current)
        new_hours = schedule_hours(schedule, name, settings) - old_hours + paid(shift)
        if new_hours > target_hours(name) + tol + allow_over_target:
            return False
        if not shift_fits_availability(shift, name, day, availability_df):
            return False
        return True

    def candidate_shifts_for(name: str, day: str, closing: bool, desired_paid: Optional[float] = None) -> List[Shift]:
        area = emp_row[name]["Area"]
        options: List[Shift] = []
        pool_candidates = pool.get((area, day, closing), []) + ([] if closing else pool.get((area, day, True), []))
        # Keep order/frequency from pool but dedupe by raw string.
        seen_raw = set()
        for sh in pool_candidates:
            if sh and sh.raw not in seen_raw:
                seen_raw.add(sh.raw)
                options.append(sh)
        if desired_paid is None:
            desired_paid = max(target_hours(name) - schedule_hours(schedule, name, settings), 2.0)
        dyn = dynamic_shift_from_availability(name, day, availability_df, desired_paid, settings, closing=closing)
        if dyn and dyn.raw not in seen_raw:
            options.insert(0, dyn)
        # Filter by desired closing/non-closing when requested.
        filtered = []
        for sh in options:
            is_close = shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4))
            if closing and not is_close:
                continue
            filtered.append(sh)
        return filtered

    def trim_to_target(name: str) -> bool:
        """Remove a non-critical shift if the employee is above target/rest limits."""
        changed = False
        guard = 0
        while guard < 20:
            guard += 1
            hours = schedule_hours(schedule, name, settings)
            if hours <= target_hours(name) + tol and schedule_off_days(schedule, name) >= int(emp_row[name]["Min Descansos"]):
                return changed
            candidates = []
            for day in DAYS:
                sh = schedule[name].get(day)
                if sh is None or (name, day) in locked_work:
                    continue
                critical = 1 if removal_breaks_req(name, day) else 0
                closes = 1 if shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)) else 0
                excess_after = abs((hours - paid(sh)) - target_hours(name))
                candidates.append((critical, closes, excess_after, -paid(sh), day))
            safe = [c for c in candidates if c[0] == 0]
            candidates = safe or candidates
            if not candidates:
                break
            candidates.sort()
            day_to_remove = candidates[0][-1]
            schedule[name][day_to_remove] = None
            changed = True
        return changed

    def make_room_for(name: str, needed_hours: float, protected_day: str) -> bool:
        """Free hours for an assignment by removing another non-critical shift."""
        current = schedule_hours(schedule, name, settings)
        if current + needed_hours <= target_hours(name) + tol:
            return True
        candidates = []
        for day in DAYS:
            if day == protected_day or (name, day) in locked_work:
                continue
            sh = schedule[name].get(day)
            if sh is None:
                continue
            if removal_breaks_req(name, day):
                continue
            closes = 1 if shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)) else 0
            candidates.append((closes, abs((current - paid(sh) + needed_hours) - target_hours(name)), day))
        if not candidates:
            return False
        candidates.sort()
        schedule[name][candidates[0][2]] = None
        return True

    # First pass: reduce people already above target/rest limits without destroying critical coverage.
    for name in list(schedule.keys()):
        trim_to_target(name)

    areas = sorted(active["Area"].dropna().unique().tolist())

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
            # Prefer replacing a non-closing same-day shift for closing coverage, then assigning an OFF day.
            replace_bonus = 0
            if current_shift is not None:
                if not closing or shift_closes(current_shift, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)):
                    continue
                replace_bonus = -2
            for sh in candidate_shifts_for(name, day, closing=closing):
                old_hours = paid(current_shift)
                add_hours = paid(sh) - old_hours
                if add_hours > 0 and schedule_hours(schedule, name, settings) + add_hours > target_hours(name) + tol:
                    make_room_for(name, add_hours, day)
                if can_place(name, day, sh, allow_replace_nonclosing=True, allow_over_target=0.0):
                    base_sh = base_map.get((name, day))
                    base_close_bonus = -1 if base_sh and shift_closes(base_sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)) else 0
                    under = max(target_hours(name) - schedule_hours(schedule, name, settings), 0)
                    candidates.append((replace_bonus + base_close_bonus, -under, schedule_hours(schedule, name, settings), name, sh))
                    break
        if not candidates:
            missing = min_needed - current_count
            warnings.append(f"No se pudo cubrir {'cierre' if closing else 'dotación'}: {day} / {area}. Faltan {missing}.")
            return False
        candidates.sort()
        _, _, _, name, shift = candidates[0]
        schedule[name][day] = shift
        return True

    # Repair closing coverage first. This handles the case where a closer asked for rest.
    for day in DAYS:
        for area in areas:
            _, min_closers = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, True, settings) < min_closers and guard < 20:
                guard += 1
                if not assign_to_cover(day, area, closing=True):
                    break

    # Repair general coverage after closures.
    for day in DAYS:
        for area in areas:
            min_total, _ = req_for(day, area)
            guard = 0
            while count_coverage(schedule, active, day, area, False, settings) < min_total and guard < 20:
                guard += 1
                if not assign_to_cover(day, area, closing=False):
                    break

    def try_extend_existing_shift(name: str, needed: float) -> bool:
        # Extend one existing non-locked shift inside availability when no extra workday is available.
        for day in DAYS:
            if (name, day) in locked_off or (name, day) in locked_work:
                continue
            current = schedule[name].get(day)
            if current is None:
                continue
            desired_paid = paid(current) + needed
            dyn = dynamic_shift_from_availability(name, day, availability_df, desired_paid, settings, closing=shift_closes(current, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)))
            if dyn is None:
                continue
            new_hours = schedule_hours(schedule, name, settings) - paid(current) + paid(dyn)
            if new_hours <= target_hours(name) + tol and shift_fits_availability(dyn, name, day, availability_df):
                schedule[name][day] = dyn
                return True
        return False

    # Complete weekly hours. This is the key behavior for PT/FT: request off => reassign elsewhere if possible.
    for name, row in emp_row.items():
        guard = 0
        while schedule_hours(schedule, name, settings) < target_hours(name) - tol and guard < 30:
            guard += 1
            remaining = target_hours(name) - schedule_hours(schedule, name, settings)
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
                # Prefer days where this employee usually worked or where their area needs stronger coverage.
                base_bonus = 0 if base_map.get((name, day)) else 1
                current_area_coverage = count_coverage(schedule, active, day, row["Area"], False, settings)
                possible_days.append((base_bonus, current_area_coverage, day))
            possible_days.sort()

            assigned = False
            for _, _, day in possible_days:
                # Use a non-closing shift to complete hours unless closing coverage is still needed.
                _, min_closers = req_for(day, row["Area"])
                closing_needed = count_coverage(schedule, active, day, row["Area"], True, settings) < min_closers
                options = candidate_shifts_for(name, day, closing=closing_needed, desired_paid=remaining)
                if not options and not closing_needed:
                    options = candidate_shifts_for(name, day, closing=True, desired_paid=remaining)
                # Try smaller remaining durations too.
                if not options:
                    dyn = dynamic_shift_from_availability(name, day, availability_df, remaining, settings, closing=False)
                    options = [dyn] if dyn else []
                for sh in options:
                    if sh is None:
                        continue
                    add = paid(sh)
                    if add <= 0:
                        continue
                    if schedule_hours(schedule, name, settings) + add > target_hours(name) + tol:
                        # Try creating a shorter custom shift for the exact remaining hours.
                        custom = dynamic_shift_from_availability(name, day, availability_df, remaining, settings, closing=shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)), min_paid_hours=1.0)
                        if custom and schedule_hours(schedule, name, settings) + paid(custom) <= target_hours(name) + tol:
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
                if try_extend_existing_shift(name, remaining):
                    assigned = True
                else:
                    warnings.append(f"{name} queda con {schedule_hours(schedule, name, settings)}h de {target_hours(name)}h. No hay disponibilidad suficiente para completar horas.")
                    break

    # Final trimming if coverage repair created excess.
    for name in list(schedule.keys()):
        trim_to_target(name)

    # One last coverage pass in case trimming opened a gap.
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

    long_rows = []
    for _, row in active.iterrows():
        name, area, turno = row["Trabajador"], row["Area"], row["Turno"]
        for day in DAYS:
            sh = schedule[name].get(day)
            long_rows.append({
                "Trabajador": name,
                "Area": area,
                "Turno": turno,
                "Dia": day,
                "Shift": format_shift(sh),
                "Hora Inicio": minutes_to_hhmm(sh.start_min) if sh else "",
                "Hora Fin": minutes_to_hhmm(sh.end_min) if sh else "",
                "Raw Hours": round(sh.raw_hours if sh else 0, 2),
                "Horas Pagadas": round(paid(sh), 2),
                "Cierre": shift_closes(sh, settings.get("close_from_hour", 1), settings.get("close_to_hour", 4)),
            })
    out_long = pd.DataFrame(long_rows)
    out_wide = long_to_wide(out_long)
    summary = build_summary(out_long, active)

    for _, row in summary.iterrows():
        target = float(row["Max Horas"])
        if row["Horas Generadas"] > target + tol:
            warnings.append(f"{row['Trabajador']} supera horas objetivo: {row['Horas Generadas']}h > {target}h.")
        if row["Horas Generadas"] < target - tol:
            warnings.append(f"{row['Trabajador']} no completa horas objetivo: {row['Horas Generadas']}h / {target}h.")
        if row["Descansos"] < row["Min Descansos"]:
            warnings.append(f"{row['Trabajador']} no cumple descansos mínimos: {row['Descansos']} < {row['Min Descansos']}.")
    if requirements_df is not None and not requirements_df.empty:
        for _, req in requirements_df.iterrows():
            day, area = normalize_day(req["Dia"]), normalize_area(req["Area"])
            min_total, min_close = int(req.get("Min Personas", 0) or 0), int(req.get("Min Cierres", 0) or 0)
            total = count_coverage(schedule, active, day, area, False, settings)
            close = count_coverage(schedule, active, day, area, True, settings)
            if total < min_total:
                warnings.append(f"Cobertura insuficiente {day}/{area}: {total}/{min_total} personas.")
            if close < min_close:
                warnings.append(f"Cierres insuficientes {day}/{area}: {close}/{min_close} cierres.")

    seen = set()
    all_messages = request_notes + warnings
    deduped = [w for w in all_messages if not (w in seen or seen.add(w))]
    return out_wide, out_long, summary, deduped

def build_summary(long_df: pd.DataFrame, employees_df: pd.DataFrame) -> pd.DataFrame:
    workdays = long_df[long_df["Shift"] != "OFF"].groupby("Trabajador")["Dia"].nunique().to_dict()
    hours = long_df.groupby("Trabajador")["Horas Pagadas"].sum().round(2).to_dict()
    closes = long_df.groupby("Trabajador")["Cierre"].sum().to_dict()
    result = employees_df[["Trabajador", "Area", "Turno", "Max Horas", "Min Descansos"]].copy()
    result["Horas Generadas"] = result["Trabajador"].map(hours).fillna(0).round(2)
    result["Dias Trabajados"] = result["Trabajador"].map(workdays).fillna(0).astype(int)
    result["Descansos"] = 7 - result["Dias Trabajados"]
    result["Cierres"] = result["Trabajador"].map(closes).fillna(0).astype(int)
    return result


def export_schedule_excel(wide_df: pd.DataFrame, long_df: pd.DataFrame, summary_df: pd.DataFrame, warnings: List[str]) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        wide_df.to_excel(writer, sheet_name="Horario Semanal", index=False)
        long_df.to_excel(writer, sheet_name="Formato Largo", index=False)
        summary_df.to_excel(writer, sheet_name="Resumen", index=False)
        pd.DataFrame({"Validaciones": warnings or ["Sin advertencias"]}).to_excel(writer, sheet_name="Validaciones", index=False)
        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                max_len = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 12), 32)
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True)
    return output.getvalue()
