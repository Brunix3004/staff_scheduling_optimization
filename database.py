from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from scheduler_core import (
    DAYS,
    build_availability_from_base,
    build_requirements_from_base,
    build_summary,
    long_to_wide,
    normalize_area,
    normalize_contract,
    normalize_day,
    normalize_input_excel,
    normalize_key,
    normalize_text,
)

DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "bembos_scheduler.db"


def get_connection(path: Path | str = DB_PATH) -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            recovery_hash TEXT NOT NULL,
            recovery_salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'ADMIN',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            user_agent TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            area TEXT NOT NULL CHECK(area IN ('SERVICIO', 'PRODUCCION')),
            contract_type TEXT NOT NULL CHECK(contract_type IN ('PT', 'FT')),
            active INTEGER NOT NULL DEFAULT 1,
            max_hours REAL NOT NULL,
            min_rest_days INTEGER NOT NULL,
            base_hours REAL DEFAULT 0,
            base_work_days INTEGER DEFAULT 0,
            base_rest_days INTEGER DEFAULT 0,
            comment TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            available INTEGER NOT NULL DEFAULT 1,
            observation TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            area TEXT NOT NULL CHECK(area IN ('SERVICIO', 'PRODUCCION')),
            min_people INTEGER NOT NULL DEFAULT 0,
            min_closers INTEGER NOT NULL DEFAULT 0,
            UNIQUE(day, area)
        );

        CREATE TABLE IF NOT EXISTS special_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            request_date TEXT NOT NULL,
            request_type TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            comment TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ACTIVA',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'GENERADO',
            notes TEXT DEFAULT '',
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,
            is_base INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS schedule_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            day TEXT NOT NULL,
            shift TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            raw_hours REAL NOT NULL DEFAULT 0,
            paid_hours REAL NOT NULL DEFAULT 0,
            closing INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS schedule_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
            warning TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            imported_at TEXT NOT NULL,
            rows_employees INTEGER NOT NULL DEFAULT 0,
            rows_availability INTEGER NOT NULL DEFAULT 0,
            rows_requests INTEGER NOT NULL DEFAULT 0,
            rows_schedule INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        """
    )
    defaults = {
        "pt_hours": "19",
        "ft_hours": "48",
        "pt_rest": "2",
        "ft_rest": "1",
        "break_after_hours": "6",
        "break_minutes": "45",
        "hour_tolerance": "0.25",
        "ft_infer_threshold": "35",
        "close_from_hour": "1",
        "close_to_hour": "4",
    }
    for key, value in defaults.items():
        con.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (key, value))

    # Migration for the new Bembos closing rule:
    # closing = shift crosses midnight and ends at/after 01:00.
    current_close_from = con.execute("SELECT value FROM app_settings WHERE key='close_from_hour'").fetchone()
    current_close_to = con.execute("SELECT value FROM app_settings WHERE key='close_to_hour'").fetchone()
    if current_close_from and str(current_close_from["value"]) == "23":
        con.execute("UPDATE app_settings SET value='1' WHERE key='close_from_hour'")
    if current_close_to and str(current_close_to["value"]) == "2":
        con.execute("UPDATE app_settings SET value='4' WHERE key='close_to_hour'")
    con.commit()


# ----------------------------- Auth helpers -----------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash_secret(secret: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt = base64.b64encode(salt_bytes).decode("utf-8")
    else:
        salt_bytes = base64.b64decode(salt.encode("utf-8"))
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt_bytes, 150_000)
    return base64.b64encode(digest).decode("utf-8"), salt


def _verify_secret(secret: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_secret(secret, salt)
    return secrets.compare_digest(candidate, stored_hash)


def generate_recovery_code() -> str:
    raw = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
    return f"BEMBOS-{raw[:6]}-{raw[6:12]}".upper()


def count_users(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_user(con: sqlite3.Connection, username: str, email: str, password: str, role: str = "ADMIN") -> str:
    username = normalize_text(username)
    email = normalize_text(email).lower()
    if len(username) < 3:
        raise ValueError("El usuario debe tener al menos 3 caracteres.")
    if "@" not in email:
        raise ValueError("Correo inválido.")
    if len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres.")
    password_hash, salt = _hash_secret(password)
    recovery_code = generate_recovery_code()
    recovery_hash, recovery_salt = _hash_secret(recovery_code)
    con.execute(
        """
        INSERT INTO users(username, email, password_hash, salt, recovery_hash, recovery_salt, role, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (username, email, password_hash, salt, recovery_hash, recovery_salt, role.upper(), _now()),
    )
    con.commit()
    return recovery_code


def authenticate_user(con: sqlite3.Connection, username_or_email: str, password: str) -> Optional[dict]:
    key = normalize_text(username_or_email).lower()
    row = con.execute(
        "SELECT * FROM users WHERE lower(username)=? OR lower(email)=?", (key, key)
    ).fetchone()
    if not row or not int(row["active"]):
        return None
    if not _verify_secret(password, row["password_hash"], row["salt"]):
        return None
    con.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), row["id"]))
    con.commit()
    return {"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]}


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_login_session(con: sqlite3.Connection, user_id: int, hours: int = 12, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    con.execute(
        """
        INSERT INTO sessions(user_id, token_hash, created_at, expires_at, user_agent)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, _session_hash(token), _now(), expires_at, normalize_text(user_agent)),
    )
    con.commit()
    return token


def user_from_session(con: sqlite3.Connection, token: str) -> Optional[dict]:
    token = normalize_text(token)
    if not token:
        return None
    row = con.execute(
        """
        SELECT u.id, u.username, u.email, u.role, u.active, s.expires_at, s.revoked_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash=?
        """,
        (_session_hash(token),),
    ).fetchone()
    if not row or not int(row["active"]) or row["revoked_at"]:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return None
    except Exception:
        return None
    return {"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]}


def revoke_login_session(con: sqlite3.Connection, token: str) -> None:
    token = normalize_text(token)
    if token:
        con.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (_now(), _session_hash(token)))
        con.commit()


def cleanup_expired_sessions(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM sessions WHERE expires_at < ? OR revoked_at IS NOT NULL", (_now(),))
    con.commit()


def change_password(con: sqlite3.Connection, user_id: int, current_password: str, new_password: str) -> None:
    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not _verify_secret(current_password, row["password_hash"], row["salt"]):
        raise ValueError("La contraseña actual no es correcta.")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    password_hash, salt = _hash_secret(new_password)
    con.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (password_hash, salt, user_id))
    con.commit()


def reset_password_with_code(con: sqlite3.Connection, username_or_email: str, recovery_code: str, new_password: str) -> None:
    key = normalize_text(username_or_email).lower()
    row = con.execute("SELECT * FROM users WHERE lower(username)=? OR lower(email)=?", (key, key)).fetchone()
    if not row:
        raise ValueError("No se encontró el usuario.")
    if not _verify_secret(normalize_text(recovery_code).upper(), row["recovery_hash"], row["recovery_salt"]):
        raise ValueError("El código de recuperación no es correcto.")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    password_hash, salt = _hash_secret(new_password)
    con.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (password_hash, salt, row["id"]))
    con.commit()


def rotate_recovery_code(con: sqlite3.Connection, user_id: int) -> str:
    code = generate_recovery_code()
    recovery_hash, recovery_salt = _hash_secret(code)
    con.execute("UPDATE users SET recovery_hash=?, recovery_salt=? WHERE id=?", (recovery_hash, recovery_salt, user_id))
    con.commit()
    return code


# ----------------------------- Settings -----------------------------

def get_settings(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT key, value FROM app_settings").fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    return {
        "pt_hours": float(raw.get("pt_hours", 19)),
        "ft_hours": float(raw.get("ft_hours", 48)),
        "pt_rest": int(float(raw.get("pt_rest", 2))),
        "ft_rest": int(float(raw.get("ft_rest", 1))),
        "break_after_hours": float(raw.get("break_after_hours", 6)),
        "break_minutes": int(float(raw.get("break_minutes", 45))),
        "hour_tolerance": float(raw.get("hour_tolerance", 0.25)),
        "ft_infer_threshold": float(raw.get("ft_infer_threshold", 35)),
        "close_from_hour": int(float(raw.get("close_from_hour", 1))),
        "close_to_hour": int(float(raw.get("close_to_hour", 4))),
    }


def save_settings(con: sqlite3.Connection, settings: dict) -> None:
    for key, value in settings.items():
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    con.commit()


# ----------------------------- DataFrame helpers -----------------------------

def has_business_data(con: sqlite3.Connection) -> bool:
    return int(con.execute("SELECT COUNT(*) FROM employees").fetchone()[0]) > 0


def employees_df(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT name AS Trabajador, area AS Area, contract_type AS Turno, active AS Activo,
               max_hours AS [Max Horas], min_rest_days AS [Min Descansos],
               base_hours AS [Horas Semana Base], base_work_days AS [Dias Trabajados Base],
               base_rest_days AS [Descansos Base], comment AS Comentario
        FROM employees ORDER BY area, name
        """,
        con,
    )
    if not df.empty:
        df["Activo"] = df["Activo"].astype(bool)
    return df


def availability_df(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT e.name AS Trabajador, a.day AS Dia, a.start_time AS [Hora Inicio], a.end_time AS [Hora Fin],
               a.available AS Disponible, a.observation AS Observacion
        FROM availability a JOIN employees e ON e.id = a.employee_id
        ORDER BY e.name, CASE a.day
            WHEN 'LUNES' THEN 1 WHEN 'MARTES' THEN 2 WHEN 'MIERCOLES' THEN 3 WHEN 'JUEVES' THEN 4
            WHEN 'VIERNES' THEN 5 WHEN 'SABADO' THEN 6 WHEN 'DOMINGO' THEN 7 ELSE 8 END, a.start_time
        """,
        con,
    )
    if not df.empty:
        df["Disponible"] = df["Disponible"].astype(bool)
    return df


def requirements_df(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT day AS Dia, area AS Area, min_people AS [Min Personas], min_closers AS [Min Cierres]
        FROM requirements
        ORDER BY CASE day
            WHEN 'LUNES' THEN 1 WHEN 'MARTES' THEN 2 WHEN 'MIERCOLES' THEN 3 WHEN 'JUEVES' THEN 4
            WHEN 'VIERNES' THEN 5 WHEN 'SABADO' THEN 6 WHEN 'DOMINGO' THEN 7 ELSE 8 END, area
        """,
        con,
    )


def requests_df(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT e.name AS Trabajador, sr.request_date AS Fecha, sr.request_type AS [Tipo Solicitud],
               sr.comment AS Comentario
        FROM special_requests sr JOIN employees e ON e.id = sr.employee_id
        WHERE sr.status='ACTIVA'
        ORDER BY sr.request_date DESC, e.name
        """,
        con,
    )


def schedules_df(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT s.id, s.week_start AS [Inicio Semana], s.week_end AS [Fin Semana], s.source AS Fuente,
               s.notes AS Notas, s.is_base AS Base, s.created_at AS [Creado]
        FROM schedules s ORDER BY s.week_start DESC, s.id DESC
        """,
        con,
    )


def _get_employee_id(con: sqlite3.Connection, name: str) -> Optional[int]:
    row = con.execute("SELECT id FROM employees WHERE name=?", (normalize_text(name),)).fetchone()
    return int(row["id"]) if row else None


def replace_employees(con: sqlite3.Connection, df: pd.DataFrame) -> None:
    now = _now()
    clean = df.copy().fillna("")
    required_cols = ["Trabajador", "Area", "Turno", "Activo", "Max Horas", "Min Descansos"]
    for col in required_cols:
        if col not in clean.columns:
            raise ValueError(f"Falta columna {col} en colaboradores.")
    for _, row in clean.iterrows():
        name = normalize_text(row.get("Trabajador"))
        if not name:
            continue
        area = normalize_area(row.get("Area"))
        turno = normalize_contract(row.get("Turno")) or "PT"
        active = 1 if bool(row.get("Activo", True)) and normalize_key(row.get("Activo")) not in {"FALSE", "0", "NO", "INACTIVO"} else 0
        max_hours = float(row.get("Max Horas") or (19 if turno == "PT" else 48))
        min_rest = int(float(row.get("Min Descansos") or (2 if turno == "PT" else 1)))
        base_hours = float(row.get("Horas Semana Base") or 0)
        base_work_days = int(float(row.get("Dias Trabajados Base") or 0))
        base_rest_days = int(float(row.get("Descansos Base") or 0))
        comment = normalize_text(row.get("Comentario"))
        con.execute(
            """
            INSERT INTO employees(name, area, contract_type, active, max_hours, min_rest_days, base_hours, base_work_days, base_rest_days, comment, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET area=excluded.area, contract_type=excluded.contract_type,
                active=excluded.active, max_hours=excluded.max_hours, min_rest_days=excluded.min_rest_days,
                base_hours=excluded.base_hours, base_work_days=excluded.base_work_days, base_rest_days=excluded.base_rest_days,
                comment=excluded.comment, updated_at=excluded.updated_at
            """,
            (name, area, turno, active, max_hours, min_rest, base_hours, base_work_days, base_rest_days, comment, now, now),
        )
    con.commit()


def replace_availability(con: sqlite3.Connection, df: pd.DataFrame) -> None:
    con.execute("DELETE FROM availability")
    now = _now()
    if df is None or df.empty:
        con.commit()
        return
    clean = df.copy().fillna("")
    for _, row in clean.iterrows():
        name = normalize_text(row.get("Trabajador"))
        day = normalize_day(row.get("Dia"))
        if not name or day not in DAYS:
            continue
        emp_id = _get_employee_id(con, name)
        if emp_id is None:
            continue
        start = normalize_text(row.get("Hora Inicio"))
        end = normalize_text(row.get("Hora Fin"))
        available = 1 if bool(row.get("Disponible", True)) and normalize_key(row.get("Disponible")) not in {"FALSE", "0", "NO", "NO DISPONIBLE"} else 0
        obs = normalize_text(row.get("Observacion")) or normalize_text(row.get("OBSERVACION"))
        con.execute(
            """
            INSERT INTO availability(employee_id, day, start_time, end_time, available, observation, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (emp_id, day, start or None, end or None, available, obs, now),
        )
    con.commit()


def replace_requirements(con: sqlite3.Connection, df: pd.DataFrame) -> None:
    con.execute("DELETE FROM requirements")
    if df is None or df.empty:
        con.commit()
        return
    clean = df.copy()
    for _, row in clean.iterrows():
        day = normalize_day(row.get("Dia"))
        area = normalize_area(row.get("Area"))
        if day not in DAYS or area not in {"SERVICIO", "PRODUCCION"}:
            continue
        con.execute(
            """
            INSERT INTO requirements(day, area, min_people, min_closers) VALUES (?, ?, ?, ?)
            ON CONFLICT(day, area) DO UPDATE SET min_people=excluded.min_people, min_closers=excluded.min_closers
            """,
            (day, area, int(row.get("Min Personas") or 0), int(row.get("Min Cierres") or 0)),
        )
    con.commit()


def replace_requests(con: sqlite3.Connection, df: pd.DataFrame) -> None:
    con.execute("DELETE FROM special_requests")
    if df is None or df.empty:
        con.commit()
        return
    clean = df.copy().fillna("")
    for _, row in clean.iterrows():
        name = normalize_text(row.get("Trabajador") or row.get("COLABORADOR"))
        if not name:
            continue
        emp_id = _get_employee_id(con, name)
        if emp_id is None:
            continue
        fecha = row.get("Fecha") or row.get("FECHA")
        if isinstance(fecha, (datetime, date)):
            fecha_str = fecha.date().isoformat() if isinstance(fecha, datetime) else fecha.isoformat()
        else:
            parsed = _parse_date_cell(fecha)
            fecha_str = parsed.isoformat() if parsed else ""
        if not fecha_str:
            continue
        tipo = normalize_key(row.get("Tipo Solicitud") or row.get("Tipo") or row.get("TIPO_SOLICITUD")) or "NO_TRABAJA"
        status = normalize_key(row.get("Estado")) or "ACTIVA"
        con.execute(
            """
            INSERT INTO special_requests(employee_id, request_date, request_type, start_time, end_time, comment, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (emp_id, fecha_str, tipo, None, None, normalize_text(row.get("Comentario") or row.get("COMENTARIO")), status, _now()),
        )
    con.commit()


def requests_for_week(con: sqlite3.Connection, week_start: date) -> pd.DataFrame:
    week_end = week_start + timedelta(days=6)
    df = pd.read_sql_query(
        """
        SELECT e.name AS Trabajador, sr.request_date AS Fecha, sr.request_type AS Tipo,
               sr.start_time AS [Hora Inicio], sr.end_time AS [Hora Fin], sr.comment AS Comentario
        FROM special_requests sr JOIN employees e ON e.id = sr.employee_id
        WHERE sr.status='ACTIVA' AND sr.request_date BETWEEN ? AND ?
        ORDER BY sr.request_date, e.name
        """,
        con,
        params=(week_start.isoformat(), week_end.isoformat()),
    )
    if df.empty:
        return pd.DataFrame(columns=["Trabajador", "Dia", "Tipo", "Hora Inicio", "Hora Fin", "Comentario"])
    date_to_day = { (week_start + timedelta(days=i)).isoformat(): DAYS[i] for i in range(7) }
    df["Dia"] = df["Fecha"].astype(str).map(date_to_day)
    return df[["Trabajador", "Dia", "Tipo", "Hora Inicio", "Hora Fin", "Comentario"]]


def save_schedule(
    con: sqlite3.Connection,
    wide_df: pd.DataFrame,
    long_df: pd.DataFrame,
    warnings: list[str],
    week_start: date,
    source: str,
    notes: str = "",
    created_by: Optional[int] = None,
    is_base: bool = False,
) -> int:
    week_end = week_start + timedelta(days=6)
    cur = con.execute(
        """
        INSERT INTO schedules(week_start, week_end, source, notes, created_by, created_at, is_base)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (week_start.isoformat(), week_end.isoformat(), source, notes, created_by, _now(), 1 if is_base else 0),
    )
    schedule_id = int(cur.lastrowid)
    clean = long_df.copy().fillna("")
    for _, row in clean.iterrows():
        emp_id = _get_employee_id(con, row.get("Trabajador"))
        if emp_id is None:
            continue
        con.execute(
            """
            INSERT INTO schedule_entries(schedule_id, employee_id, day, shift, start_time, end_time, raw_hours, paid_hours, closing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                emp_id,
                normalize_day(row.get("Dia")),
                normalize_text(row.get("Shift")) or "OFF",
                normalize_text(row.get("Hora Inicio")) or None,
                normalize_text(row.get("Hora Fin")) or None,
                float(row.get("Raw Hours") or 0),
                float(row.get("Horas Pagadas") or 0),
                1 if bool(row.get("Cierre")) else 0,
            ),
        )
    for warning in warnings or []:
        con.execute("INSERT INTO schedule_warnings(schedule_id, warning) VALUES (?, ?)", (schedule_id, str(warning)))
    con.commit()
    return schedule_id


def schedule_long_df(con: sqlite3.Connection, schedule_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT e.name AS Trabajador, e.area AS Area, e.contract_type AS Turno, se.day AS Dia,
               se.shift AS Shift, COALESCE(se.start_time, '') AS [Hora Inicio], COALESCE(se.end_time, '') AS [Hora Fin],
               se.raw_hours AS [Raw Hours], se.paid_hours AS [Horas Pagadas], se.closing AS Cierre
        FROM schedule_entries se JOIN employees e ON e.id = se.employee_id
        WHERE se.schedule_id=?
        ORDER BY e.area, e.name, CASE se.day
            WHEN 'LUNES' THEN 1 WHEN 'MARTES' THEN 2 WHEN 'MIERCOLES' THEN 3 WHEN 'JUEVES' THEN 4
            WHEN 'VIERNES' THEN 5 WHEN 'SABADO' THEN 6 WHEN 'DOMINGO' THEN 7 ELSE 8 END
        """,
        con,
        params=(schedule_id,),
    )
    if not df.empty:
        df["Cierre"] = df["Cierre"].astype(bool)
    return df


def schedule_warnings(con: sqlite3.Connection, schedule_id: int) -> list[str]:
    rows = con.execute("SELECT warning FROM schedule_warnings WHERE schedule_id=? ORDER BY id", (schedule_id,)).fetchall()
    return [r["warning"] for r in rows]


def schedule_label(row: pd.Series | sqlite3.Row) -> str:
    return f"#{row['id']} | {row['Inicio Semana']} a {row['Fin Semana']} | {row['Fuente']}"


def latest_schedule_id(con: sqlite3.Connection) -> Optional[int]:
    row = con.execute("SELECT id FROM schedules ORDER BY week_start DESC, id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


# ----------------------------- Excel import -----------------------------

def _excel_sheet_df(xls: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    lower = {str(s).lower().strip(): s for s in xls.sheet_names}
    if sheet_name.lower() not in lower:
        return pd.DataFrame()
    df = pd.read_excel(xls, sheet_name=lower[sheet_name.lower()], dtype=object)
    df = df.dropna(how="all")
    df.columns = [normalize_text(c) for c in df.columns]
    return df


def _find_col(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    lookup = {normalize_key(c): c for c in df.columns}
    for n in names:
        if normalize_key(n) in lookup:
            return lookup[normalize_key(n)]
    return None


def _parse_date_cell(value) -> Optional[date]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = normalize_text(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _read_colaboradores_sheet(xls: pd.ExcelFile, base_employees: pd.DataFrame, settings: dict) -> pd.DataFrame:
    df = _excel_sheet_df(xls, "colaboradores")
    if df.empty:
        return base_employees
    c_name = _find_col(df, ["COLABORADOR", "TRABAJADOR", "NOMBRE"])
    c_area = _find_col(df, ["AREA", "ÁREA"])
    c_turno = _find_col(df, ["TURNO", "TIPO", "CONTRATO"])
    c_estado = _find_col(df, ["ESTADO", "ACTIVO"])
    c_comment = _find_col(df, ["COMENTARIO", "OBSERVACION", "OBSERVACIÓN"])
    if not c_name or not c_area:
        return base_employees
    base_by_name = base_employees.set_index("Trabajador").to_dict("index") if not base_employees.empty else {}
    rows = []
    for _, r in df.iterrows():
        name = normalize_text(r.get(c_name))
        if not name:
            continue
        base = base_by_name.get(name, {})
        area = normalize_area(r.get(c_area)) or base.get("Area", "SERVICIO")
        turno = normalize_contract(r.get(c_turno)) if c_turno else ""
        turno = turno or base.get("Turno") or "PT"
        estado = normalize_key(r.get(c_estado)) if c_estado else "ACTIVO"
        active = estado not in {"INACTIVO", "BAJA", "CESADO", "NO", "FALSE", "0"}
        rows.append({
            "Trabajador": name,
            "Area": area,
            "Turno": turno,
            "Activo": active,
            "Max Horas": float(base.get("Max Horas", settings.get("pt_hours", 19) if turno == "PT" else settings.get("ft_hours", 48))),
            "Min Descansos": int(base.get("Min Descansos", settings.get("pt_rest", 2) if turno == "PT" else settings.get("ft_rest", 1))),
            "Horas Semana Base": float(base.get("Horas Semana Base", 0)),
            "Dias Trabajados Base": int(base.get("Dias Trabajados Base", 0)),
            "Descansos Base": int(base.get("Descansos Base", 0)),
            "Comentario": normalize_text(r.get(c_comment)) if c_comment else "",
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return base_employees
    # Include people present in horario_base but missing from colaboradores.
    missing = base_employees[~base_employees["Trabajador"].isin(out["Trabajador"])] if not base_employees.empty else pd.DataFrame()
    if not missing.empty:
        missing = missing.copy()
        if "Comentario" not in missing.columns:
            missing["Comentario"] = "Importado desde horario_base"
        out = pd.concat([out, missing], ignore_index=True)
    return out


def _read_disponibilidad_sheet(xls: pd.ExcelFile) -> pd.DataFrame:
    df = _excel_sheet_df(xls, "disponibilidad")
    if df.empty or len(df) == 0:
        return pd.DataFrame()
    c_name = _find_col(df, ["COLABORADOR", "TRABAJADOR", "NOMBRE"])
    c_day = _find_col(df, ["DIA", "DÍA"])
    c_start = _find_col(df, ["DESDE", "HORA INICIO", "INICIO", "ENTRADA"])
    c_end = _find_col(df, ["HASTA", "HORA FIN", "FIN", "SALIDA"])
    c_obs = _find_col(df, ["OBSERVACION", "OBSERVACIÓN", "COMENTARIO"])
    if not c_name or not c_day:
        return pd.DataFrame()
    rows = []
    for _, r in df.iterrows():
        name = normalize_text(r.get(c_name))
        day = normalize_day(r.get(c_day))
        if not name or day not in DAYS:
            continue
        start = normalize_text(r.get(c_start)) if c_start else ""
        end = normalize_text(r.get(c_end)) if c_end else ""
        available = bool(start and end and normalize_key(start) not in {"NO DISPONIBLE", "OFF", "NULL"})
        rows.append({
            "Trabajador": name,
            "Dia": day,
            "Hora Inicio": start if available else "",
            "Hora Fin": end if available else "",
            "Disponible": available,
            "Observacion": normalize_text(r.get(c_obs)) if c_obs else "",
        })
    return pd.DataFrame(rows)


def _read_solicitudes_sheet(xls: pd.ExcelFile) -> pd.DataFrame:
    df = _excel_sheet_df(xls, "solicitudes")
    empty = pd.DataFrame(columns=["Trabajador", "Fecha", "Tipo Solicitud", "Comentario"])
    if df.empty:
        return empty
    c_name = _find_col(df, ["COLABORADOR", "TRABAJADOR", "NOMBRE"])
    c_date = _find_col(df, ["FECHA"])
    c_type = _find_col(df, ["TIPO_SOLICITUD", "TIPO", "SOLICITUD"])
    c_comment = _find_col(df, ["COMENTARIO", "OBSERVACION", "OBSERVACIÓN"])
    if not c_name or not c_date:
        return empty
    rows = []
    for _, r in df.iterrows():
        name = normalize_text(r.get(c_name))
        fecha = _parse_date_cell(r.get(c_date))
        if not name or not fecha:
            continue
        rows.append({
            "Trabajador": name,
            "Fecha": fecha.isoformat(),
            "Tipo Solicitud": normalize_key(r.get(c_type)) if c_type else "NO_TRABAJA",
            "Comentario": normalize_text(r.get(c_comment)) if c_comment else "",
        })
    return pd.DataFrame(rows, columns=["Trabajador", "Fecha", "Tipo Solicitud", "Comentario"])


def extract_week_start_from_workbook(file_or_path) -> date:
    try:
        xls = pd.ExcelFile(file_or_path)
        df = _excel_sheet_df(xls, "horario_base")
        c_start = _find_col(df, ["INICIO_SEMANA", "SEMANA", "INICIO SEMANA"])
        if c_start and not df.empty:
            dt = _parse_date_cell(df[c_start].dropna().iloc[0])
            if dt:
                return dt
    except Exception:
        pass
    today = date.today()
    return today - timedelta(days=today.weekday())


def import_initial_workbook(con: sqlite3.Connection, file_or_path, filename: str, settings: dict, created_by: Optional[int] = None, overwrite: bool = False) -> dict:
    if overwrite:
        con.executescript(
            """
            DELETE FROM schedule_warnings;
            DELETE FROM schedule_entries;
            DELETE FROM schedules;
            DELETE FROM special_requests;
            DELETE FROM availability;
            DELETE FROM requirements;
            DELETE FROM employees;
            """
        )
        con.commit()
    employees_base, base_long, base_wide = normalize_input_excel(file_or_path, settings=settings)
    xls = pd.ExcelFile(file_or_path)
    employees = _read_colaboradores_sheet(xls, employees_base, settings)
    # Apply current settings after reading/inference.
    employees["Turno"] = employees["Turno"].map(lambda x: normalize_contract(x) or "PT")
    employees["Max Horas"] = employees["Turno"].map({"PT": settings.get("pt_hours", 19), "FT": settings.get("ft_hours", 48)}).astype(float)
    employees["Min Descansos"] = employees["Turno"].map({"PT": settings.get("pt_rest", 2), "FT": settings.get("ft_rest", 1)}).astype(int)
    replace_employees(con, employees)

    availability = _read_disponibilidad_sheet(xls)
    if availability.empty:
        availability = build_availability_from_base(base_long)
        availability["Observacion"] = "Generado desde horario_base"
    replace_availability(con, availability)

    req = build_requirements_from_base(base_long)
    replace_requirements(con, req)

    requests = _read_solicitudes_sheet(xls)
    if not requests.empty:
        replace_requests(con, requests)

    week_start = extract_week_start_from_workbook(file_or_path)
    schedule_id = save_schedule(
        con,
        base_wide,
        base_long,
        [],
        week_start=week_start,
        source="IMPORTADO",
        notes=f"Carga inicial desde {filename}",
        created_by=created_by,
        is_base=True,
    )
    con.execute(
        """
        INSERT INTO import_logs(filename, imported_at, rows_employees, rows_availability, rows_requests, rows_schedule, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (filename, _now(), len(employees), len(availability), len(requests), len(base_long), f"schedule_id={schedule_id}"),
    )
    con.commit()
    return {
        "employees": len(employees),
        "availability": len(availability),
        "requests": len(requests),
        "schedule_entries": len(base_long),
        "schedule_id": schedule_id,
        "week_start": week_start.isoformat(),
    }

