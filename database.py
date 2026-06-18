"""
database.py – Capa de persistencia PostgreSQL para Bembos Scheduler
====================================================================
Migrado de SQLite a PostgreSQL (Supabase free tier).
- Misma API pública que la versión SQLite: el app.py no cambia casi nada.
- Placeholders: %s en lugar de ?
- Autoincrement: SERIAL en lugar de INTEGER PRIMARY KEY AUTOINCREMENT
- ON CONFLICT: sintaxis PostgreSQL estándar
- No existe PRAGMA ni executescript → se usan bloques SQL separados
- La conexión se obtiene de DATABASE_URL (variable de entorno en HF Spaces)
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
import psycopg2
import psycopg2.extras

from scheduler_core import (
    DAYS,
    build_availability_from_base,
    build_requirements_from_base,
    build_summary,
    format_paid_hours,
    long_to_wide,
    normalize_area,
    normalize_contract,
    normalize_day,
    normalize_input_excel,
    normalize_key,
    normalize_text,
)


# ─────────────────────────────────────────────────────────────────────────────
# Conexión
# ─────────────────────────────────────────────────────────────────────────────

def get_connection() -> psycopg2.extensions.connection:
    """
    Retorna una conexión PostgreSQL.
    Lee DATABASE_URL del entorno (configurada como Secret en Hugging Face Spaces).
    Formato: postgresql://user:password@host:port/dbname
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Variable de entorno DATABASE_URL no encontrada. "
            "Configúrala en los Secrets de tu Hugging Face Space."
        )
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def _exec(con, sql: str, params=None):
    cur = con.cursor()
    cur.execute(sql, params or ())
    return cur


def _fetchone(con, sql: str, params=None) -> Optional[dict]:
    cur = _exec(con, sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _fetchall(con, sql: str, params=None) -> list[dict]:
    cur = _exec(con, sql, params)
    return [dict(r) for r in cur.fetchall()]


def _read_sql(con, sql: str, params=None) -> pd.DataFrame:
    """Wrapper para pd.read_sql compatible con psycopg2."""
    return pd.read_sql(sql, con, params=params)


# ─────────────────────────────────────────────────────────────────────────────
# Inicialización / migraciones
# ─────────────────────────────────────────────────────────────────────────────

def init_db(con) -> None:
    """Crea todas las tablas si no existen. Idempotente."""
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            username      TEXT    NOT NULL UNIQUE,
            email         TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            salt          TEXT    NOT NULL,
            recovery_hash TEXT    NOT NULL,
            recovery_salt TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'ADMIN',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT    NOT NULL,
            last_login    TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT    NOT NULL UNIQUE,
            created_at TEXT    NOT NULL,
            expires_at TEXT    NOT NULL,
            revoked_at TEXT,
            user_agent TEXT    DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS employees (
            id             SERIAL PRIMARY KEY,
            name           TEXT    NOT NULL UNIQUE,
            area           TEXT    NOT NULL,
            contract_type  TEXT    NOT NULL,
            active         INTEGER NOT NULL DEFAULT 1,
            max_hours      REAL    NOT NULL DEFAULT 19,
            max_minutes    INTEGER NOT NULL DEFAULT 1140,
            min_rest_days  INTEGER NOT NULL DEFAULT 2,
            base_hours     REAL    DEFAULT 0,
            base_work_days INTEGER DEFAULT 0,
            base_rest_days INTEGER DEFAULT 0,
            comment        TEXT    DEFAULT '',
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS availability (
            id          SERIAL PRIMARY KEY,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            day         TEXT    NOT NULL,
            start_time  TEXT,
            end_time    TEXT,
            available   INTEGER NOT NULL DEFAULT 1,
            observation TEXT    DEFAULT '',
            updated_at  TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS requirements (
            id          SERIAL PRIMARY KEY,
            day         TEXT    NOT NULL,
            area        TEXT    NOT NULL,
            min_people  INTEGER NOT NULL DEFAULT 0,
            min_closers INTEGER NOT NULL DEFAULT 0,
            UNIQUE(day, area)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS special_requests (
            id           SERIAL PRIMARY KEY,
            employee_id  INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            request_date TEXT    NOT NULL,
            request_type TEXT    NOT NULL,
            start_time   TEXT,
            end_time     TEXT,
            comment      TEXT    DEFAULT '',
            status       TEXT    NOT NULL DEFAULT 'ACTIVA',
            created_at   TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id         SERIAL PRIMARY KEY,
            week_start TEXT    NOT NULL,
            week_end   TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'GENERADO',
            notes      TEXT    DEFAULT '',
            created_by INTEGER REFERENCES users(id),
            created_at TEXT    NOT NULL,
            is_base    INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedule_entries (
            id           SERIAL PRIMARY KEY,
            schedule_id  INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
            employee_id  INTEGER NOT NULL REFERENCES employees(id),
            day          TEXT    NOT NULL,
            shift        TEXT    NOT NULL,
            start_time   TEXT,
            end_time     TEXT,
            raw_minutes  INTEGER NOT NULL DEFAULT 0,
            paid_minutes INTEGER NOT NULL DEFAULT 0,
            paid_hours   TEXT    NOT NULL DEFAULT '00:00',
            closing      INTEGER NOT NULL DEFAULT 0,
            opening      INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedule_warnings (
            id          SERIAL PRIMARY KEY,
            schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
            warning     TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS import_logs (
            id                SERIAL PRIMARY KEY,
            filename          TEXT,
            imported_at       TEXT    NOT NULL,
            rows_employees    INTEGER NOT NULL DEFAULT 0,
            rows_availability INTEGER NOT NULL DEFAULT 0,
            rows_requests     INTEGER NOT NULL DEFAULT 0,
            rows_schedule     INTEGER NOT NULL DEFAULT 0,
            notes             TEXT    DEFAULT ''
        )
        """,
    ]
    for stmt in stmts:
        _exec(con, stmt)

    # Valores por defecto de configuración
    defaults = {
        "pt_hours": "19", "ft_hours": "48",
        "pt_rest": "2",   "ft_rest": "1",
        "break_after_hours": "6", "break_minutes": "45",
        "hour_tolerance": "0.25", "ft_infer_threshold": "35",
        "close_from_hour": "1",   "close_to_hour": "4",
    }
    for key, value in defaults.items():
        _exec(con,
              "INSERT INTO app_settings(key, value) VALUES (%s, %s) ON CONFLICT(key) DO NOTHING",
              (key, value))
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

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


def count_users(con) -> int:
    row = _fetchone(con, "SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def create_user(con, username: str, email: str, password: str, role: str = "ADMIN") -> str:
    username = normalize_text(username)
    email    = normalize_text(email).lower()
    if len(username) < 3:
        raise ValueError("El usuario debe tener al menos 3 caracteres.")
    if "@" not in email:
        raise ValueError("Correo inválido.")
    if len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres.")
    pw_hash, salt           = _hash_secret(password)
    recovery_code           = generate_recovery_code()
    rec_hash, rec_salt      = _hash_secret(recovery_code)
    _exec(con,
          """INSERT INTO users(username,email,password_hash,salt,recovery_hash,recovery_salt,role,active,created_at)
             VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s)""",
          (username, email, pw_hash, salt, rec_hash, rec_salt, role.upper(), _now()))
    con.commit()
    return recovery_code


def authenticate_user(con, username_or_email: str, password: str) -> Optional[dict]:
    key = normalize_text(username_or_email).lower()
    row = _fetchone(con,
                    "SELECT * FROM users WHERE lower(username)=%s OR lower(email)=%s",
                    (key, key))
    if not row or not int(row["active"]):
        return None
    if not _verify_secret(password, row["password_hash"], row["salt"]):
        return None
    _exec(con, "UPDATE users SET last_login=%s WHERE id=%s", (_now(), row["id"]))
    con.commit()
    return {"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]}


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_login_session(con, user_id: int, hours: int = 12, user_agent: str = "") -> str:
    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    _exec(con,
          "INSERT INTO sessions(user_id,token_hash,created_at,expires_at,user_agent) VALUES (%s,%s,%s,%s,%s)",
          (user_id, _session_hash(token), _now(), expires_at, normalize_text(user_agent)))
    con.commit()
    return token


def user_from_session(con, token: str) -> Optional[dict]:
    token = normalize_text(token)
    if not token:
        return None
    row = _fetchone(con,
                    """SELECT u.id,u.username,u.email,u.role,u.active,s.expires_at,s.revoked_at
                       FROM sessions s JOIN users u ON u.id=s.user_id
                       WHERE s.token_hash=%s""",
                    (_session_hash(token),))
    if not row or not int(row["active"]) or row["revoked_at"]:
        return None
    try:
        if datetime.fromisoformat(str(row["expires_at"])) < datetime.now():
            return None
    except Exception:
        return None
    return {"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]}


def revoke_login_session(con, token: str) -> None:
    token = normalize_text(token)
    if token:
        _exec(con, "UPDATE sessions SET revoked_at=%s WHERE token_hash=%s", (_now(), _session_hash(token)))
        con.commit()


def cleanup_expired_sessions(con) -> None:
    _exec(con, "DELETE FROM sessions WHERE expires_at < %s OR revoked_at IS NOT NULL", (_now(),))
    con.commit()


def change_password(con, user_id: int, current_password: str, new_password: str) -> None:
    row = _fetchone(con, "SELECT * FROM users WHERE id=%s", (user_id,))
    if not row or not _verify_secret(current_password, row["password_hash"], row["salt"]):
        raise ValueError("La contraseña actual no es correcta.")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    pw_hash, salt = _hash_secret(new_password)
    _exec(con, "UPDATE users SET password_hash=%s,salt=%s WHERE id=%s", (pw_hash, salt, user_id))
    con.commit()


def reset_password_with_code(con, username_or_email: str, recovery_code: str, new_password: str) -> None:
    key = normalize_text(username_or_email).lower()
    row = _fetchone(con, "SELECT * FROM users WHERE lower(username)=%s OR lower(email)=%s", (key, key))
    if not row:
        raise ValueError("No se encontró el usuario.")
    if not _verify_secret(normalize_text(recovery_code).upper(), row["recovery_hash"], row["recovery_salt"]):
        raise ValueError("El código de recuperación no es correcto.")
    if len(new_password) < 8:
        raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    pw_hash, salt = _hash_secret(new_password)
    _exec(con, "UPDATE users SET password_hash=%s,salt=%s WHERE id=%s", (pw_hash, salt, row["id"]))
    con.commit()


def rotate_recovery_code(con, user_id: int) -> str:
    code           = generate_recovery_code()
    rec_hash, rec_salt = _hash_secret(code)
    _exec(con, "UPDATE users SET recovery_hash=%s,recovery_salt=%s WHERE id=%s", (rec_hash, rec_salt, user_id))
    con.commit()
    return code


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def get_settings(con) -> dict:
    rows = _fetchall(con, "SELECT key, value FROM app_settings")
    raw  = {r["key"]: r["value"] for r in rows}
    return {
        "pt_hours":           float(raw.get("pt_hours",           19)),
        "ft_hours":           float(raw.get("ft_hours",           48)),
        "pt_rest":            int(float(raw.get("pt_rest",         2))),
        "ft_rest":            int(float(raw.get("ft_rest",         1))),
        "break_after_hours":  float(raw.get("break_after_hours",   6)),
        "break_minutes":      int(float(raw.get("break_minutes",  45))),
        "hour_tolerance":     float(raw.get("hour_tolerance",    0.25)),
        "ft_infer_threshold": float(raw.get("ft_infer_threshold",  35)),
        "close_from_hour":    int(float(raw.get("close_from_hour",  1))),
        "close_to_hour":      int(float(raw.get("close_to_hour",    4))),
    }


def save_settings(con, settings: dict) -> None:
    for key, value in settings.items():
        _exec(con,
              "INSERT INTO app_settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
              (key, str(value)))
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Ventana de solicitudes
# ─────────────────────────────────────────────────────────────────────────────

def request_window_open(for_week_start: date) -> bool:
    now         = datetime.now()
    publish_day = for_week_start - timedelta(days=7)
    deadline    = datetime.combine(publish_day + timedelta(days=2), datetime.min.time()).replace(hour=12)
    open_from   = datetime.combine(publish_day, datetime.min.time())
    return open_from <= now <= deadline


def request_window_info(for_week_start: date) -> dict:
    publish_day = for_week_start - timedelta(days=7)
    deadline    = datetime.combine(publish_day + timedelta(days=2), datetime.min.time()).replace(hour=12)
    open_from   = datetime.combine(publish_day, datetime.min.time())
    now         = datetime.now()
    return {"open_from": open_from, "deadline": deadline, "is_open": open_from <= now <= deadline, "now": now}


# ─────────────────────────────────────────────────────────────────────────────
# DataFrame helpers (lectura)
# ─────────────────────────────────────────────────────────────────────────────

_DAY_ORDER = "CASE day WHEN 'LUNES' THEN 1 WHEN 'MARTES' THEN 2 WHEN 'MIERCOLES' THEN 3 WHEN 'JUEVES' THEN 4 WHEN 'VIERNES' THEN 5 WHEN 'SABADO' THEN 6 WHEN 'DOMINGO' THEN 7 ELSE 8 END"


def has_business_data(con) -> bool:
    row = _fetchone(con, "SELECT COUNT(*) AS n FROM employees")
    return int(row["n"]) > 0 if row else False


def employees_df(con) -> pd.DataFrame:
    rows = _fetchall(con,
        "SELECT name,area,contract_type,active,max_hours,max_minutes,min_rest_days,"
        "base_hours,base_work_days,base_rest_days,comment FROM employees ORDER BY area,name")
    if not rows:
        return pd.DataFrame(columns=["Trabajador","Area","Turno","Activo","Max Horas","Max Minutos",
                                     "Min Descansos","Horas Semana Base","Dias Trabajados Base",
                                     "Descansos Base","Comentario"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "name": "Trabajador", "area": "Area", "contract_type": "Turno", "active": "Activo",
        "max_hours": "Max Horas", "max_minutes": "Max Minutos", "min_rest_days": "Min Descansos",
        "base_hours": "Horas Semana Base", "base_work_days": "Dias Trabajados Base",
        "base_rest_days": "Descansos Base", "comment": "Comentario",
    })
    df["Activo"] = df["Activo"].astype(bool)
    return df


def availability_df(con) -> pd.DataFrame:
    rows = _fetchall(con,
        f"SELECT e.name,a.day,a.start_time,a.end_time,a.available,a.observation "
        f"FROM availability a JOIN employees e ON e.id=a.employee_id "
        f"ORDER BY e.name, {_DAY_ORDER.replace('day','a.day')}, a.start_time")
    if not rows:
        return pd.DataFrame(columns=["Trabajador","Dia","Hora Inicio","Hora Fin","Disponible","Observacion"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={"name":"Trabajador","day":"Dia","start_time":"Hora Inicio",
                             "end_time":"Hora Fin","available":"Disponible","observation":"Observacion"})
    df["Disponible"] = df["Disponible"].astype(bool)
    return df


def requirements_df(con) -> pd.DataFrame:
    rows = _fetchall(con,
        f"SELECT day,area,min_people,min_closers FROM requirements "
        f"ORDER BY {_DAY_ORDER}, area")
    if not rows:
        return pd.DataFrame(columns=["Dia","Area","Min Personas","Min Cierres"])
    df = pd.DataFrame(rows)
    return df.rename(columns={"day":"Dia","area":"Area","min_people":"Min Personas","min_closers":"Min Cierres"})


def requests_df(con) -> pd.DataFrame:
    rows = _fetchall(con,
        "SELECT e.name,sr.request_date,sr.request_type,sr.comment,sr.status "
        "FROM special_requests sr JOIN employees e ON e.id=sr.employee_id "
        "WHERE sr.status='ACTIVA' ORDER BY sr.request_date DESC, e.name")
    if not rows:
        return pd.DataFrame(columns=["Trabajador","Fecha","Tipo Solicitud","Comentario","Estado"])
    df = pd.DataFrame(rows)
    return df.rename(columns={"name":"Trabajador","request_date":"Fecha",
                               "request_type":"Tipo Solicitud","comment":"Comentario","status":"Estado"})


def schedules_df(con) -> pd.DataFrame:
    rows = _fetchall(con,
        "SELECT id,week_start,week_end,source,notes,is_base,created_at FROM schedules "
        "ORDER BY week_start DESC, id DESC")
    if not rows:
        return pd.DataFrame(columns=["id","Inicio Semana","Fin Semana","Fuente","Notas","Base","Creado"])
    df = pd.DataFrame(rows)
    return df.rename(columns={"week_start":"Inicio Semana","week_end":"Fin Semana","source":"Fuente",
                               "notes":"Notas","is_base":"Base","created_at":"Creado"})


def _get_employee_id(con, name: str) -> Optional[int]:
    row = _fetchone(con, "SELECT id FROM employees WHERE name=%s", (normalize_text(name),))
    return int(row["id"]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Escritura de datos
# ─────────────────────────────────────────────────────────────────────────────

def replace_employees(con, df: pd.DataFrame) -> None:
    now   = _now()
    clean = df.copy().fillna("")
    for col in ["Trabajador", "Area", "Turno", "Activo", "Max Horas", "Min Descansos"]:
        if col not in clean.columns:
            raise ValueError(f"Falta columna {col}.")
    for _, row in clean.iterrows():
        name = normalize_text(row.get("Trabajador"))
        if not name:
            continue
        area      = normalize_area(row.get("Area"))
        turno     = normalize_contract(row.get("Turno")) or "PT"
        active_raw = row.get("Activo", True)
        active = 1 if bool(active_raw) and normalize_key(str(active_raw)) not in {"FALSE","0","NO","INACTIVO"} else 0
        max_h  = float(row.get("Max Horas") or (19 if turno == "PT" else 48))
        max_m  = int(row.get("Max Minutos") or int(max_h * 60))
        min_r  = int(float(row.get("Min Descansos") or (2 if turno == "PT" else 1)))
        base_h = float(row.get("Horas Semana Base") or 0)
        base_wd = int(float(row.get("Dias Trabajados Base") or 0))
        base_rd = int(float(row.get("Descansos Base") or 0))
        comment = normalize_text(row.get("Comentario"))
        _exec(con,
              """INSERT INTO employees(name,area,contract_type,active,max_hours,max_minutes,
                 min_rest_days,base_hours,base_work_days,base_rest_days,comment,created_at,updated_at)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT(name) DO UPDATE SET
                   area=EXCLUDED.area, contract_type=EXCLUDED.contract_type,
                   active=EXCLUDED.active, max_hours=EXCLUDED.max_hours,
                   max_minutes=EXCLUDED.max_minutes, min_rest_days=EXCLUDED.min_rest_days,
                   base_hours=EXCLUDED.base_hours, base_work_days=EXCLUDED.base_work_days,
                   base_rest_days=EXCLUDED.base_rest_days, comment=EXCLUDED.comment,
                   updated_at=EXCLUDED.updated_at""",
              (name, area, turno, active, max_h, max_m, min_r, base_h, base_wd, base_rd, comment, now, now))
    con.commit()


def replace_availability(con, df: pd.DataFrame) -> None:
    _exec(con, "DELETE FROM availability")
    now = _now()
    if df is None or df.empty:
        con.commit()
        return
    clean = df.copy().fillna("")
    for _, row in clean.iterrows():
        name = normalize_text(row.get("Trabajador"))
        day  = normalize_day(row.get("Dia"))
        if not name or day not in DAYS:
            continue
        emp_id = _get_employee_id(con, name)
        if emp_id is None:
            continue
        start     = normalize_text(row.get("Hora Inicio"))
        end       = normalize_text(row.get("Hora Fin"))
        avail_raw = row.get("Disponible", True)
        available = 1 if bool(avail_raw) and normalize_key(str(avail_raw)) not in {"FALSE","0","NO","NO DISPONIBLE"} else 0
        obs = normalize_text(row.get("Observacion")) or normalize_text(row.get("OBSERVACION"))
        _exec(con,
              "INSERT INTO availability(employee_id,day,start_time,end_time,available,observation,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
              (emp_id, day, start or None, end or None, available, obs, now))
    con.commit()


def replace_requirements(con, df: pd.DataFrame) -> None:
    _exec(con, "DELETE FROM requirements")
    if df is None or df.empty:
        con.commit()
        return
    for _, row in df.iterrows():
        day  = normalize_day(row.get("Dia"))
        area = normalize_area(row.get("Area"))
        if day not in DAYS or area not in {"SERVICIO", "PRODUCCION"}:
            continue
        _exec(con,
              "INSERT INTO requirements(day,area,min_people,min_closers) VALUES (%s,%s,%s,%s) "
              "ON CONFLICT(day,area) DO UPDATE SET min_people=EXCLUDED.min_people,min_closers=EXCLUDED.min_closers",
              (day, area, int(row.get("Min Personas") or 0), int(row.get("Min Cierres") or 0)))
    con.commit()


def replace_requests(con, df: pd.DataFrame) -> None:
    _exec(con, "DELETE FROM special_requests")
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
            parsed    = _parse_date_cell(fecha)
            fecha_str = parsed.isoformat() if parsed else ""
        if not fecha_str:
            continue
        tipo   = normalize_key(row.get("Tipo Solicitud") or row.get("Tipo") or row.get("TIPO_SOLICITUD")) or "NO_TRABAJA"
        status = normalize_key(row.get("Estado")) or "ACTIVA"
        _exec(con,
              "INSERT INTO special_requests(employee_id,request_date,request_type,start_time,end_time,comment,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
              (emp_id, fecha_str, tipo, None, None,
               normalize_text(row.get("Comentario") or row.get("COMENTARIO")), status, _now()))
    con.commit()


def requests_for_week(con, week_start: date) -> pd.DataFrame:
    week_end = week_start + timedelta(days=6)
    rows = _fetchall(con,
        "SELECT e.name,sr.request_date,sr.request_type,sr.start_time,sr.end_time,sr.comment "
        "FROM special_requests sr JOIN employees e ON e.id=sr.employee_id "
        "WHERE sr.status='ACTIVA' AND sr.request_date BETWEEN %s AND %s ORDER BY sr.request_date,e.name",
        (week_start.isoformat(), week_end.isoformat()))
    if not rows:
        return pd.DataFrame(columns=["Trabajador","Dia","Tipo","Hora Inicio","Hora Fin","Comentario"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={"name":"Trabajador","request_date":"Fecha","request_type":"Tipo",
                             "start_time":"Hora Inicio","end_time":"Hora Fin","comment":"Comentario"})
    date_to_day = {(week_start + timedelta(days=i)).isoformat(): DAYS[i] for i in range(7)}
    df["Dia"] = df["Fecha"].astype(str).map(date_to_day)
    return df[["Trabajador","Dia","Tipo","Hora Inicio","Hora Fin","Comentario"]]


def save_schedule(con, wide_df: pd.DataFrame, long_df: pd.DataFrame, warnings: list,
                  week_start: date, source: str, notes: str = "",
                  created_by: Optional[int] = None, is_base: bool = False) -> int:
    week_end = week_start + timedelta(days=6)
    cur = _exec(con,
                "INSERT INTO schedules(week_start,week_end,source,notes,created_by,created_at,is_base) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (week_start.isoformat(), week_end.isoformat(), source, notes, created_by, _now(), 1 if is_base else 0))
    schedule_id = cur.fetchone()["id"]

    clean = long_df.copy().fillna("")
    for _, row in clean.iterrows():
        emp_id = _get_employee_id(con, row.get("Trabajador"))
        if emp_id is None:
            continue
        paid_m = int(row.get("Minutos Pagados") or 0)
        raw_m  = int(row.get("Minutos Brutos")  or 0)
        ph     = format_paid_hours(paid_m)
        _exec(con,
              "INSERT INTO schedule_entries(schedule_id,employee_id,day,shift,start_time,end_time,"
              "raw_minutes,paid_minutes,paid_hours,closing,opening) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
              (schedule_id, emp_id,
               normalize_day(row.get("Dia")),
               normalize_text(row.get("Shift")) or "OFF",
               normalize_text(row.get("Hora Inicio")) or None,
               normalize_text(row.get("Hora Fin"))    or None,
               raw_m, paid_m, ph,
               1 if bool(row.get("Cierre"))   else 0,
               1 if bool(row.get("Apertura")) else 0))
    for w in warnings or []:
        _exec(con, "INSERT INTO schedule_warnings(schedule_id,warning) VALUES (%s,%s)", (schedule_id, str(w)))
    con.commit()
    return schedule_id


def schedule_long_df(con, schedule_id: int) -> pd.DataFrame:
    rows = _fetchall(con,
        f"SELECT e.name,e.area,e.contract_type,se.day,se.shift,COALESCE(se.start_time,'') AS start_time,"
        f"COALESCE(se.end_time,'') AS end_time,se.raw_minutes,se.paid_minutes,se.paid_hours,se.closing,se.opening "
        f"FROM schedule_entries se JOIN employees e ON e.id=se.employee_id "
        f"WHERE se.schedule_id=%s ORDER BY e.area,e.name, {_DAY_ORDER.replace('day','se.day')}",
        (schedule_id,))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "name":"Trabajador","area":"Area","contract_type":"Turno","day":"Dia","shift":"Shift",
        "start_time":"Hora Inicio","end_time":"Hora Fin","raw_minutes":"Minutos Brutos",
        "paid_minutes":"Minutos Pagados","paid_hours":"Horas Pagadas","closing":"Cierre","opening":"Apertura"
    })
    df["Cierre"]   = df["Cierre"].astype(bool)
    df["Apertura"] = df["Apertura"].astype(bool)
    return df


def schedule_warnings(con, schedule_id: int) -> list[str]:
    rows = _fetchall(con, "SELECT warning FROM schedule_warnings WHERE schedule_id=%s ORDER BY id", (schedule_id,))
    return [r["warning"] for r in rows]


def schedule_label(row) -> str:
    return f"#{row['id']} | {row['Inicio Semana']} → {row['Fin Semana']} | {row['Fuente']}"


def latest_schedule_id(con) -> Optional[int]:
    row = _fetchone(con, "SELECT id FROM schedules ORDER BY week_start DESC, id DESC LIMIT 1")
    return int(row["id"]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Importación desde Excel (sin cambios de lógica)
# ─────────────────────────────────────────────────────────────────────────────

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


def _read_colaboradores_sheet(xls, base_employees, settings):
    df = _excel_sheet_df(xls, "colaboradores")
    if df.empty:
        return base_employees
    c_name    = _find_col(df, ["COLABORADOR","TRABAJADOR","NOMBRE"])
    c_area    = _find_col(df, ["AREA","ÁREA"])
    c_turno   = _find_col(df, ["TURNO","TIPO","CONTRATO"])
    c_estado  = _find_col(df, ["ESTADO","ACTIVO"])
    c_comment = _find_col(df, ["COMENTARIO","OBSERVACION","OBSERVACIÓN"])
    if not c_name or not c_area:
        return base_employees
    base_by_name = base_employees.set_index("Trabajador").to_dict("index") if not base_employees.empty else {}
    rows = []
    for _, r in df.iterrows():
        name = normalize_text(r.get(c_name))
        if not name:
            continue
        base  = base_by_name.get(name, {})
        area  = normalize_area(r.get(c_area)) or base.get("Area","SERVICIO")
        turno = normalize_contract(r.get(c_turno)) if c_turno else ""
        turno = turno or base.get("Turno") or "PT"
        estado = normalize_key(r.get(c_estado)) if c_estado else "ACTIVO"
        active = estado not in {"INACTIVO","BAJA","CESADO","NO","FALSE","0"}
        max_h  = float(base.get("Max Horas") or (settings.get("pt_hours",19) if turno=="PT" else settings.get("ft_hours",48)))
        max_m  = int(max_h * 60)
        rows.append({
            "Trabajador": name, "Area": area, "Turno": turno, "Activo": active,
            "Max Horas": max_h, "Max Minutos": max_m,
            "Min Descansos": int(base.get("Min Descansos") or (settings.get("pt_rest",2) if turno=="PT" else settings.get("ft_rest",1))),
            "Horas Semana Base": float(base.get("Horas Semana Base") or 0),
            "Dias Trabajados Base": int(base.get("Dias Trabajados Base") or 0),
            "Descansos Base": int(base.get("Descansos Base") or 0),
            "Comentario": normalize_text(r.get(c_comment)) if c_comment else "",
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return base_employees
    missing = base_employees[~base_employees["Trabajador"].isin(out["Trabajador"])] if not base_employees.empty else pd.DataFrame()
    if not missing.empty:
        missing = missing.copy()
        if "Comentario" not in missing.columns:
            missing["Comentario"] = "Importado desde horario_base"
        out = pd.concat([out, missing], ignore_index=True)
    return out


def _read_disponibilidad_sheet(xls):
    df = _excel_sheet_df(xls, "disponibilidad")
    if df.empty:
        return pd.DataFrame()
    c_name  = _find_col(df, ["COLABORADOR","TRABAJADOR","NOMBRE"])
    c_day   = _find_col(df, ["DIA","DÍA"])
    c_start = _find_col(df, ["DESDE","HORA INICIO","INICIO","ENTRADA"])
    c_end   = _find_col(df, ["HASTA","HORA FIN","FIN","SALIDA"])
    c_obs   = _find_col(df, ["OBSERVACION","OBSERVACIÓN","COMENTARIO"])
    if not c_name or not c_day:
        return pd.DataFrame()
    rows = []
    for _, r in df.iterrows():
        name = normalize_text(r.get(c_name))
        day  = normalize_day(r.get(c_day))
        if not name or day not in DAYS:
            continue
        start     = normalize_text(r.get(c_start)) if c_start else ""
        end       = normalize_text(r.get(c_end))   if c_end   else ""
        available = bool(start and end and normalize_key(start) not in {"NO DISPONIBLE","OFF","NULL"})
        rows.append({"Trabajador":name,"Dia":day,"Hora Inicio":start if available else "",
                     "Hora Fin":end if available else "","Disponible":available,
                     "Observacion":normalize_text(r.get(c_obs)) if c_obs else ""})
    return pd.DataFrame(rows)


def _read_solicitudes_sheet(xls):
    df    = _excel_sheet_df(xls, "solicitudes")
    empty = pd.DataFrame(columns=["Trabajador","Fecha","Tipo Solicitud","Comentario"])
    if df.empty:
        return empty
    c_name    = _find_col(df, ["COLABORADOR","TRABAJADOR","NOMBRE"])
    c_date    = _find_col(df, ["FECHA"])
    c_type    = _find_col(df, ["TIPO_SOLICITUD","TIPO","SOLICITUD"])
    c_comment = _find_col(df, ["COMENTARIO","OBSERVACION","OBSERVACIÓN"])
    if not c_name or not c_date:
        return empty
    rows = []
    for _, r in df.iterrows():
        name  = normalize_text(r.get(c_name))
        fecha = _parse_date_cell(r.get(c_date))
        if not name or not fecha:
            continue
        rows.append({"Trabajador":name,"Fecha":fecha.isoformat(),
                     "Tipo Solicitud":normalize_key(r.get(c_type)) if c_type else "NO_TRABAJA",
                     "Comentario":normalize_text(r.get(c_comment)) if c_comment else ""})
    return pd.DataFrame(rows, columns=["Trabajador","Fecha","Tipo Solicitud","Comentario"])


def extract_week_start_from_workbook(file_or_path) -> date:
    try:
        xls     = pd.ExcelFile(file_or_path)
        df      = _excel_sheet_df(xls, "horario_base")
        c_start = _find_col(df, ["INICIO_SEMANA","SEMANA","INICIO SEMANA"])
        if c_start and not df.empty:
            dt = _parse_date_cell(df[c_start].dropna().iloc[0])
            if dt:
                return dt
    except Exception:
        pass
    today = date.today()
    return today - timedelta(days=today.weekday())


def import_initial_workbook(con, file_or_path, filename: str, settings: dict,
                             created_by: Optional[int] = None, overwrite: bool = False) -> dict:
    if overwrite:
        for tbl in ["schedule_warnings","schedule_entries","schedules",
                    "special_requests","availability","requirements","employees"]:
            _exec(con, f"DELETE FROM {tbl}")
        con.commit()

    employees_base, base_long, base_wide = normalize_input_excel(file_or_path, settings=settings)
    xls       = pd.ExcelFile(file_or_path)
    employees = _read_colaboradores_sheet(xls, employees_base, settings)

    employees["Turno"]       = employees["Turno"].map(lambda x: normalize_contract(x) or "PT")
    employees["Max Horas"]   = employees["Turno"].map({"PT": settings.get("pt_hours",19), "FT": settings.get("ft_hours",48)}).astype(float)
    employees["Max Minutos"] = (employees["Max Horas"] * 60).astype(int)
    employees["Min Descansos"] = employees["Turno"].map({"PT": settings.get("pt_rest",2), "FT": settings.get("ft_rest",1)}).astype(int)

    replace_employees(con, employees)

    availability = _read_disponibilidad_sheet(xls)
    if availability.empty:
        availability = build_availability_from_base(base_long)
        availability["Observacion"] = "Generado desde horario_base"
    replace_availability(con, availability)

    req = build_requirements_from_base(base_long,
                                        close_from_hour=int(settings.get("close_from_hour",1)),
                                        close_to_hour=int(settings.get("close_to_hour",4)))
    replace_requirements(con, req)

    requests = _read_solicitudes_sheet(xls)
    if not requests.empty:
        replace_requests(con, requests)

    week_start  = extract_week_start_from_workbook(file_or_path)
    schedule_id = save_schedule(con, base_wide, base_long, [],
                                week_start=week_start, source="IMPORTADO",
                                notes=f"Carga inicial desde {filename}",
                                created_by=created_by, is_base=True)

    _exec(con,
          "INSERT INTO import_logs(filename,imported_at,rows_employees,rows_availability,rows_requests,rows_schedule,notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
          (filename, _now(), len(employees), len(availability), len(requests), len(base_long), f"schedule_id={schedule_id}"))
    con.commit()

    return {"employees": len(employees), "availability": len(availability), "requests": len(requests),
            "schedule_entries": len(base_long), "schedule_id": schedule_id, "week_start": week_start.isoformat()}
