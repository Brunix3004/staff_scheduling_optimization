"""
app.py – Bembos Scheduler
Interfaz Streamlit rediseñada para hamburguesería peruana.
Paleta: Rojo #C1121F | Mostaza #E9A21A | Crema #FFF8EF | Carbón #1A1A1A
"""
from __future__ import annotations

from datetime import date, timedelta, datetime
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from database import (
    DB_PATH,
    authenticate_user,
    availability_df,
    change_password,
    cleanup_expired_sessions,
    count_users,
    create_login_session,
    create_user,
    employees_df,
    get_connection,
    get_settings,
    has_business_data,
    import_initial_workbook,
    init_db,
    latest_schedule_id,
    replace_availability,
    replace_employees,
    replace_requirements,
    replace_requests,
    request_window_info,
    request_window_open,
    requests_df,
    requests_for_week,
    requirements_df,
    reset_password_with_code,
    revoke_login_session,
    rotate_recovery_code,
    save_schedule,
    save_settings,
    schedule_label,
    schedule_long_df,
    schedule_warnings,
    schedules_df,
    user_from_session,
)
from scheduler_core import (
    DAYS,
    PT_MAX_MINUTES,
    FT_MAX_MINUTES,
    MIN_CIERRE_PRODUCCION,
    MIN_CIERRE_SERVICIO,
    build_summary,
    export_schedule_excel,
    format_paid_hours,
    generate_schedule,
    long_to_wide,
)

# ── Configuración de página ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Bembos Scheduler",
    page_icon="🍔",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_DIR    = Path(__file__).resolve().parent
IMPORT_DIR = APP_DIR / "data" / "imports"
IMPORT_DIR.mkdir(parents=True, exist_ok=True)


def get_con():
    con = get_connection()
    init_db(con)
    return con


con = get_con()
cleanup_expired_sessions(con)


# ── Helpers de sesión ────────────────────────────────────────────────────────

def admin_registration_code() -> str:
    env_code = os.environ.get("ADMIN_REGISTRATION_CODE")
    if env_code:
        return env_code
    try:
        return str(st.secrets.get("ADMIN_REGISTRATION_CODE", "bembos-admin-2026"))
    except Exception:
        return "bembos-admin-2026"


def default_week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def next_week_start() -> date:
    return default_week_start() + timedelta(days=7)


def save_uploaded_file(uploaded_file) -> Path:
    path = IMPORT_DIR / uploaded_file.name
    path.write_bytes(uploaded_file.getvalue())
    return path


def get_session_token_from_url() -> str:
    token = st.query_params.get("session", "")
    if isinstance(token, list):
        token = token[0] if token else ""
    return str(token or "")


def set_session_token_in_url(token: str) -> None:
    st.query_params["session"] = token


def clear_session_token_from_url() -> None:
    try:
        del st.query_params["session"]
    except Exception:
        pass


# ── Estilos ──────────────────────────────────────────────────────────────────

def inject_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;900&family=Inter:wght@400;500;600&display=swap');

        /* ── Color base de texto en todo el área principal ── */
        .stApp { background-color: #FFF8EF; color: #1A1A1A; }
        .stApp p, .stApp span, .stApp li,
        .stApp div, .stApp label { color: #1A1A1A; }

        /* ── Texto en inputs, selectboxes, etc. ── */
        .stTextInput input,
        .stSelectbox div[data-baseweb="select"] *,
        .stNumberInput input,
        .stDateInput input,
        .stTextArea textarea { color: #1A1A1A !important; background: #fff !important; }

        /* ── Contenedor central ── */
        .main .block-container { padding-top: 1.2rem; max-width: 1300px; }

        /* ── Sidebar oscuro ── */
        [data-testid="stSidebar"] { background: #1A1A1A !important; border-right: 3px solid #C1121F; }
        /* Solo el texto dentro del sidebar: p, span, label, li — NO inputs */
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] li,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] .stRadio label { color: #FFF8EF !important; }

        /* ── Marca en sidebar ── */
        .brand-block {
            background: linear-gradient(135deg, #C1121F 0%, #8B0000 100%);
            border-radius: 12px; padding: 18px 16px 14px;
            margin-bottom: 16px; text-align: center;
        }
        .brand-emoji { font-size: 2.4rem; line-height: 1; }
        .brand-name {
            font-family: 'Barlow Condensed', sans-serif;
            font-weight: 900; font-size: 1.5rem; letter-spacing: 0.04em;
            color: #E9A21A !important; text-transform: uppercase; margin-top: 4px;
        }
        .brand-sub {
            font-family: 'Inter', sans-serif; font-size: 0.7rem;
            color: #FFD8A8 !important; letter-spacing: 0.08em; text-transform: uppercase;
        }

        /* ── Badge usuario en sidebar ── */
        .user-badge {
            background: #2A2A2A; border: 1px solid #C1121F; border-radius: 8px;
            padding: 8px 12px; margin-bottom: 14px;
            font-family: 'Inter', sans-serif; font-size: 0.8rem; color: #FFF8EF;
        }

        /* ── Info rápida sidebar ── */
        [data-testid="stSidebar"] .stMarkdown div { color: #aaa !important; }

        /* ── Métricas ── */
        div[data-testid="stMetric"] {
            background: #ffffff; border: 1px solid #F0D9C0;
            border-left: 4px solid #C1121F; border-radius: 10px;
            padding: 14px 16px; box-shadow: 0 2px 8px rgba(193,18,31,.07);
        }
        div[data-testid="stMetric"] label {
            font-family: 'Inter', sans-serif !important; font-size: 0.72rem !important;
            text-transform: uppercase; letter-spacing: 0.06em; color: #555 !important;
        }
        div[data-testid="stMetricValue"] {
            font-family: 'Barlow Condensed', sans-serif !important;
            font-size: 2rem !important; font-weight: 700 !important; color: #1A1A1A !important;
        }

        /* ── Títulos ── */
        h1, h2 {
            font-family: 'Barlow Condensed', sans-serif !important;
            font-weight: 900 !important; letter-spacing: 0.02em; color: #1A1A1A !important;
        }
        h3 {
            font-family: 'Barlow Condensed', sans-serif !important;
            font-weight: 700 !important; color: #C1121F !important;
        }

        /* ── Botón primario ── */
        .stButton > button[kind="primary"],
        .stButton > button[data-testid*="primary"] {
            background: #C1121F !important; color: #ffffff !important;
            border: none !important; border-radius: 8px !important;
            font-family: 'Barlow Condensed', sans-serif !important;
            font-weight: 700 !important; font-size: 1rem !important;
            letter-spacing: 0.05em; padding: 0.5rem 1.4rem !important;
        }
        .stButton > button[kind="primary"]:hover { background: #8B0000 !important; }

        /* ── Alert cards: color SIEMPRE oscuro ── */
        .alert-ok {
            background: #E8F5E9; border-left: 4px solid #2E7D32;
            border-radius: 8px; padding: 10px 14px; margin: 8px 0;
            font-size: 0.9rem; color: #1B5E20 !important;
        }
        .alert-warn {
            background: #FFF8E1; border-left: 4px solid #E9A21A;
            border-radius: 8px; padding: 10px 14px; margin: 8px 0;
            font-size: 0.9rem; color: #6D4C00 !important;
        }
        .alert-err {
            background: #FFEBEE; border-left: 4px solid #C1121F;
            border-radius: 8px; padding: 10px 14px; margin: 8px 0;
            font-size: 0.9rem; color: #7F0000 !important;
        }
        .alert-ok strong, .alert-warn strong, .alert-err strong { color: inherit !important; }

        /* ── Rule box ── */
        .rule-box {
            background: #1A1A1A; color: #FFF8EF !important;
            border-radius: 10px; padding: 14px 16px; margin-bottom: 12px;
            font-family: 'Inter', sans-serif; font-size: 0.85rem;
        }
        .rule-box strong { color: #E9A21A !important; }
        .rule-box em     { color: #FFD8A8 !important; }

        /* ── Tabs ── */
        .stTabs [data-baseweb="tab"] {
            font-family: 'Barlow Condensed', sans-serif !important;
            font-weight: 700 !important; font-size: 1rem !important;
            color: #555 !important;
        }
        .stTabs [aria-selected="true"] { color: #C1121F !important; border-bottom-color: #C1121F !important; }

        /* ── Divider ── */
        hr { border-color: #F0D9C0; }

        /* ── Streamlit info/warning/error nativos: texto oscuro ── */
        div[data-testid="stAlert"] { color: #1A1A1A !important; }

        /* ── Checkbox label ── */
        .stCheckbox label span { color: #1A1A1A !important; }

        /* ── Radio en main ── */
        .stRadio label span { color: #1A1A1A !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── Auth ─────────────────────────────────────────────────────────────────────

def require_auth() -> dict:
    if "auth_user" in st.session_state and st.session_state["auth_user"]:
        return st.session_state["auth_user"]
    url_token = get_session_token_from_url()
    if url_token:
        restored = user_from_session(con, url_token)
        if restored:
            st.session_state["auth_user"]      = restored
            st.session_state["session_token"]  = url_token
            return restored
    clear_session_token_from_url()

    inject_styles()
    st.markdown(
        """
        <div style="text-align:center; padding: 2rem 0 1rem;">
            <div style="font-size:3rem;">🍔</div>
            <div style="font-family:'Barlow Condensed',sans-serif; font-size:2.2rem;
                        font-weight:900; color:#C1121F; letter-spacing:0.04em;">
                BEMBOS SCHEDULER
            </div>
            <div style="color:#888; font-size:0.85rem; margin-top:4px;">
                Gestión de horarios — Acceso de administrador
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    first_user = count_users(con) == 0
    if first_user:
        st.info("Primera ejecución — crea el primer administrador y guarda el código de recuperación.")
        tabs = st.tabs(["Crear primer admin", "Recuperar contraseña"])
    else:
        tabs = st.tabs(["Iniciar sesión", "Registrar admin", "Recuperar contraseña"])

    def login_tab():
        with st.form("login_form"):
            username  = st.text_input("Usuario o correo")
            password  = st.text_input("Contraseña", type="password")
            submitted = st.form_submit_button("Entrar", type="primary")
            if submitted:
                user = authenticate_user(con, username, password)
                if user:
                    token = create_login_session(con, user["id"], hours=12)
                    st.session_state["auth_user"]     = user
                    st.session_state["session_token"] = token
                    set_session_token_in_url(token)
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

    def register_tab(first: bool = False):
        with st.form("register_form"):
            username  = st.text_input("Usuario")
            email     = st.text_input("Correo")
            password  = st.text_input("Contraseña", type="password")
            password2 = st.text_input("Repetir contraseña", type="password")
            code = ""
            if not first:
                code = st.text_input("Código de registro admin", type="password")
            submitted = st.form_submit_button("Crear administrador", type="primary")
            if submitted:
                if password != password2:
                    st.error("Las contraseñas no coinciden.")
                    return
                if not first and code != admin_registration_code():
                    st.error("Código de registro incorrecto.")
                    return
                try:
                    recovery = create_user(con, username, email, password, role="ADMIN")
                    st.session_state["last_recovery_code"] = recovery
                    st.session_state["admin_created_ok"]   = True
                    st.success("Administrador creado.")
                except Exception as exc:
                    st.error(f"No se pudo crear: {exc}")
        if st.session_state.get("admin_created_ok") and st.session_state.get("last_recovery_code"):
            st.warning("⚠️ Guarda este código de recuperación ahora — no se volverá a mostrar:")
            st.code(st.session_state["last_recovery_code"])
            if st.button("Ya lo copié → ir a iniciar sesión", type="primary"):
                st.session_state.pop("last_recovery_code", None)
                st.session_state.pop("admin_created_ok", None)
                st.rerun()

    def recover_tab():
        with st.form("recover_form"):
            username    = st.text_input("Usuario o correo")
            recovery    = st.text_input("Código de recuperación")
            new_pass    = st.text_input("Nueva contraseña", type="password")
            submitted   = st.form_submit_button("Restablecer contraseña")
            if submitted:
                try:
                    reset_password_with_code(con, username, recovery, new_pass)
                    st.success("Contraseña restablecida. Ya puedes iniciar sesión.")
                except Exception as exc:
                    st.error(str(exc))

    if first_user:
        with tabs[0]: register_tab(first=True)
        with tabs[1]: recover_tab()
    else:
        with tabs[0]: login_tab()
        with tabs[1]: register_tab(first=False)
        with tabs[2]: recover_tab()

    st.stop()


# ── Páginas ──────────────────────────────────────────────────────────────────

PAGE_LABELS = {
    "Inicio":               "🏠  Inicio",
    "Carga inicial":        "📥  Carga inicial",
    "Colaboradores":        "👥  Colaboradores",
    "Disponibilidad":       "🗓️  Disponibilidad",
    "Solicitudes":          "📝  Solicitudes",
    "Cobertura":            "🎯  Cobertura",
    "Generar horario":      "⚙️  Generar horario",
    "Historial":            "📚  Historial / Exportar",
    "Configuración":        "🔧  Configuración",
    "Cuenta":               "👤  Cuenta",
}
LABEL_TO_PAGE = {v: k for k, v in PAGE_LABELS.items()}


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

inject_styles()
user     = require_auth()
settings = get_settings(con)

# ── Sidebar ──────────────────────────────────────────────────────────────────
st.sidebar.markdown(
    """
    <div class="brand-block">
        <div class="brand-emoji">🍔</div>
        <div class="brand-name">Bembos</div>
        <div class="brand-sub">Gestión de Horarios</div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.sidebar.markdown(
    f"""
    <div class="user-badge">
        <span style="opacity:.6;font-size:.7rem;">ADMIN</span><br>
        <strong>{user["username"]}</strong>
    </div>
    """,
    unsafe_allow_html=True,
)

selected_label = st.sidebar.radio(
    "Menú",
    list(PAGE_LABELS.values()),
    label_visibility="collapsed",
)
page = LABEL_TO_PAGE[selected_label]

st.sidebar.divider()

# Reglas rápidas en sidebar
st.sidebar.markdown(
    f"""
    <div style="font-size:.7rem; color:#aaa; line-height:1.6; padding: 4px 2px;">
        <strong style="color:#E9A21A;">PT</strong> máx {format_paid_hours(PT_MAX_MINUTES)} h &nbsp;|&nbsp;
        <strong style="color:#E9A21A;">FT</strong> máx {format_paid_hours(FT_MAX_MINUTES)} h<br>
        Cierre mín: <strong style="color:#E9A21A;">{MIN_CIERRE_PRODUCCION}</strong> Prod +
        <strong style="color:#E9A21A;">{MIN_CIERRE_SERVICIO}</strong> Serv<br>
        Apertura: turnos 07:00 u 08:00
    </div>
    """,
    unsafe_allow_html=True,
)

st.sidebar.divider()
if st.sidebar.button("🚪 Cerrar sesión", use_container_width=True):
    token = st.session_state.get("session_token") or get_session_token_from_url()
    revoke_login_session(con, token)
    st.session_state.pop("auth_user", None)
    st.session_state.pop("session_token", None)
    clear_session_token_from_url()
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# INICIO
# ─────────────────────────────────────────────────────────────────────────────
if page == "Inicio":
    st.markdown("# 🍔 Panel de Control")

    emp    = employees_df(con)
    sched  = schedules_df(con)
    req    = requests_df(con)
    active = emp[emp["Activo"] == True] if not emp.empty else pd.DataFrame()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Colaboradores totales",  len(emp))
    c2.metric("Activos",                len(active))
    c3.metric("Horarios guardados",     len(sched))
    c4.metric("Solicitudes activas",    len(req))

    if not has_business_data(con):
        st.warning("Sin datos aún — ve a **Carga inicial** para importar el Excel.")
    else:
        st.success("Base de datos activa ✔")

    if not active.empty:
        st.markdown("### Colaboradores activos por área y tipo")
        grp = active.groupby(["Area", "Turno"], as_index=False).size().rename(columns={"size": "Cantidad"})
        st.dataframe(grp, use_container_width=True, hide_index=True)

    # Ventana de solicitudes para la semana próxima
    st.markdown("### Ventana de solicitudes")
    nws  = next_week_start()
    info = request_window_info(nws)
    if info["is_open"]:
        st.markdown(
            f"""<div class="alert-ok">
            ✅ <strong>Ventana abierta</strong> — Se aceptan solicitudes para la semana
            del <strong>{nws.strftime("%d/%m/%Y")}</strong>.<br>
            Cierre: <strong>{info["deadline"].strftime("%A %d/%m/%Y a las %H:%M")}</strong>.
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""<div class="alert-warn">
            ⏳ <strong>Ventana cerrada</strong> — Las solicitudes para la semana del
            <strong>{nws.strftime("%d/%m/%Y")}</strong> se reciben desde el
            <strong>{info["open_from"].strftime("%A %d/%m/%Y")}</strong>
            hasta el <strong>{info["deadline"].strftime("%A %d/%m/%Y %H:%M")}</strong>.
            </div>""",
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# CARGA INICIAL
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Carga inicial":
    st.markdown("# 📥 Carga inicial desde Excel")
    st.write(
        "Importa el Excel maestro una sola vez. "
        "Hojas esperadas: `colaboradores`, `disponibilidad`, `solicitudes`, `horario_base`."
    )

    st.markdown(
        """
        <div class="rule-box">
        <strong>Formato de hora en el Excel:</strong>
        Siempre <strong>HH:MM</strong> (ej. 07:00, 08:15, 16:30, 01:00).
        El valor <strong>01:00</strong> = 1 AM, no 1 minuto.
        </div>
        """,
        unsafe_allow_html=True,
    )

    uploaded  = st.file_uploader("Sube el archivo Excel", type=["xlsx", "xls"])
    overwrite = st.checkbox(
        "Sobrescribir datos actuales",
        value=not has_business_data(con),
        help="Borra colaboradores, disponibilidad, solicitudes y horarios antes de importar.",
    )

    if uploaded:
        if st.button("Importar Excel", type="primary"):
            try:
                path   = save_uploaded_file(uploaded)
                result = import_initial_workbook(
                    con, path, uploaded.name, settings,
                    created_by=user["id"], overwrite=overwrite,
                )
                st.success("✅ Excel importado correctamente.")
                st.json(result)
            except Exception as exc:
                st.error(f"Error al importar: {exc}")

    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Colaboradores",  len(employees_df(con)))
    c2.metric("Disponibilidad", len(availability_df(con)))
    c3.metric("Solicitudes",    len(requests_df(con)))
    c4.metric("Horarios",       len(schedules_df(con)))


# ─────────────────────────────────────────────────────────────────────────────
# COLABORADORES
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Colaboradores":
    st.markdown("# 👥 Colaboradores")

    st.markdown(
        """
        <div class="rule-box">
        <strong>Para dar de baja</strong> a un trabajador: desmarca <em>Activo</em> — no lo borres.
        Así conservas el historial y el motor buscará reemplazo si era cierre o apertura.<br>
        <strong>Nuevo trabajador:</strong> agrégalo aquí, llena su disponibilidad
        y el siguiente horario generado lo incluirá automáticamente.
        </div>
        """,
        unsafe_allow_html=True,
    )

    emp = employees_df(con)
    if emp.empty:
        emp = pd.DataFrame(columns=[
            "Trabajador", "Area", "Turno", "Activo",
            "Max Horas", "Max Minutos", "Min Descansos",
            "Horas Semana Base", "Dias Trabajados Base", "Descansos Base", "Comentario",
        ])

    edited = st.data_editor(
        emp,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Area":    st.column_config.SelectboxColumn("Área",  options=["SERVICIO", "PRODUCCION"], required=True),
            "Turno":   st.column_config.SelectboxColumn("Turno", options=["PT", "FT"], required=True),
            "Activo":  st.column_config.CheckboxColumn("Activo", default=True),
            "Max Horas":    st.column_config.NumberColumn("Máx Horas",    min_value=0.0, max_value=70.0, step=0.5),
            "Max Minutos":  st.column_config.NumberColumn("Máx Minutos",  min_value=0,   max_value=4200, step=15),
            "Min Descansos": st.column_config.NumberColumn("Mín Descansos", min_value=0, max_value=7,   step=1),
        },
        key="employees_editor",
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Guardar colaboradores", type="primary"):
            try:
                replace_employees(con, edited)
                st.success("Colaboradores guardados ✔")
                st.rerun()
            except Exception as exc:
                st.error(f"Error: {exc}")
    with col2:
        activos   = len(edited[edited["Activo"] == True])  if not edited.empty else 0
        inactivos = len(edited[edited["Activo"] == False]) if not edited.empty else 0
        st.info(f"**{activos}** activos · **{inactivos}** inactivos")


# ─────────────────────────────────────────────────────────────────────────────
# DISPONIBILIDAD
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Disponibilidad":
    st.markdown("# 🗓️ Disponibilidad Semanal")
    st.write("Define qué días y en qué horario puede trabajar cada colaborador. Es la base para asignar nuevos turnos.")

    st.markdown(
        """
        <div class="rule-box">
        <strong>Hora Inicio / Hora Fin:</strong> formato HH:MM (ej. 07:00, 16:30, 01:00).<br>
        Si alguien tiene dos bloques disponibles en el mismo día, crea dos filas.
        </div>
        """,
        unsafe_allow_html=True,
    )

    emp = employees_df(con)
    av  = availability_df(con)
    if av.empty:
        av = pd.DataFrame(columns=["Trabajador", "Dia", "Hora Inicio", "Hora Fin", "Disponible", "Observacion"])

    names = sorted(emp[emp["Activo"] == True]["Trabajador"].dropna().astype(str).tolist()) if not emp.empty else []

    edited = st.data_editor(
        av,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Trabajador": st.column_config.SelectboxColumn("Trabajador", options=names, required=True),
            "Dia":        st.column_config.SelectboxColumn("Día",        options=DAYS,  required=True),
            "Hora Inicio": st.column_config.TextColumn("Hora Inicio", help="HH:MM — ej. 07:00"),
            "Hora Fin":    st.column_config.TextColumn("Hora Fin",    help="HH:MM — ej. 01:00 para 1 AM"),
            "Disponible":  st.column_config.CheckboxColumn("Disponible", default=True),
        },
        key="availability_editor",
    )

    if st.button("Guardar disponibilidad", type="primary"):
        try:
            replace_availability(con, edited)
            st.success("Disponibilidad guardada ✔")
            st.rerun()
        except Exception as exc:
            st.error(f"Error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# SOLICITUDES ESPECIALES
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Solicitudes":
    st.markdown("# 📝 Solicitudes Especiales")

    # Info ventana
    nws  = next_week_start()
    info = request_window_info(nws)
    if info["is_open"]:
        st.markdown(
            f"""<div class="alert-ok">
            ✅ <strong>Ventana abierta</strong> — Se aceptan solicitudes para la semana del
            <strong>{nws.strftime("%d/%m/%Y")}</strong>.
            Cierre: <strong>{info["deadline"].strftime("%d/%m/%Y %H:%M")}</strong>.
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""<div class="alert-warn">
            ⏳ Ventana cerrada. Próxima apertura:
            <strong>{info["open_from"].strftime("%d/%m/%Y")}</strong>.
            </div>""",
            unsafe_allow_html=True,
        )

    st.markdown(
        """
        <div class="rule-box">
        <strong>Tipos de solicitud:</strong><br>
        • <strong>SOLICITA_DESCANSO</strong> — pide un día libre; el motor redistribuye sus horas otro día disponible.<br>
        • <strong>NO_TRABAJA</strong> — no trabaja ese día (sin redistribución).<br>
        • <strong>NO_DISPONIBLE</strong> — no disponible (fuerza).<br>
        • <strong>FERIADO</strong> — día feriado.<br>
        Si el trabajador cubría un cierre o apertura ese día, el sistema busca automáticamente un sustituto.
        </div>
        """,
        unsafe_allow_html=True,
    )

    emp  = employees_df(con)
    reqs = requests_df(con)
    if reqs.empty:
        reqs = pd.DataFrame({
            "Trabajador":    pd.Series(dtype="object"),
            "Fecha":         pd.Series(dtype="datetime64[ns]"),
            "Tipo Solicitud": pd.Series(dtype="object"),
            "Comentario":    pd.Series(dtype="object"),
        })
    else:
        reqs = reqs[["Trabajador", "Fecha", "Tipo Solicitud", "Comentario"]].copy()
        reqs["Fecha"] = pd.to_datetime(reqs["Fecha"], errors="coerce")

    names = sorted(emp[emp["Activo"] == True]["Trabajador"].dropna().astype(str).tolist()) if not emp.empty else []

    edited = st.data_editor(
        reqs,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Trabajador":    st.column_config.SelectboxColumn("Colaborador", options=names, required=True),
            "Fecha":         st.column_config.DateColumn("Fecha", format="DD/MM/YYYY", required=True),
            "Tipo Solicitud": st.column_config.SelectboxColumn(
                "Tipo",
                options=["SOLICITA_DESCANSO", "NO_TRABAJA", "NO_DISPONIBLE", "FERIADO"],
                required=True,
            ),
            "Comentario": st.column_config.TextColumn("Comentario"),
        },
        key="requests_editor",
    )

    if st.button("Guardar solicitudes", type="primary"):
        try:
            edited2 = edited[["Trabajador", "Fecha", "Tipo Solicitud", "Comentario"]].copy()
            replace_requests(con, edited2)
            st.success("Solicitudes guardadas ✔")
            st.rerun()
        except Exception as exc:
            st.error(f"Error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# COBERTURA
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Cobertura":
    st.markdown("# 🎯 Cobertura Requerida por Día y Área")

    st.markdown(
        f"""
        <div class="rule-box">
        <strong>Regla de cierre Bembos:</strong>
        Un turno cuenta como cierre solo si <em>cruza medianoche</em> y termina a la
        <strong>01:00 o después</strong>.<br>
        Mínimos de cierre: <strong>{MIN_CIERRE_PRODUCCION} Producción</strong> +
        <strong>{MIN_CIERRE_SERVICIO} Servicio</strong> por noche.<br>
        Aperturas: turnos que empiezan a las <strong>07:00</strong> u <strong>08:00</strong>.
        </div>
        """,
        unsafe_allow_html=True,
    )

    req = requirements_df(con)
    if req.empty:
        rows = []
        for d in DAYS:
            rows.append({"Dia": d, "Area": "PRODUCCION", "Min Personas": 0, "Min Cierres": MIN_CIERRE_PRODUCCION})
            rows.append({"Dia": d, "Area": "SERVICIO",   "Min Personas": 0, "Min Cierres": MIN_CIERRE_SERVICIO})
        req = pd.DataFrame(rows)

    edited = st.data_editor(
        req,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Dia":          st.column_config.SelectboxColumn("Día",  options=DAYS,                      required=True),
            "Area":         st.column_config.SelectboxColumn("Área", options=["SERVICIO", "PRODUCCION"], required=True),
            "Min Personas": st.column_config.NumberColumn("Mín Personas", min_value=0, max_value=50, step=1),
            "Min Cierres":  st.column_config.NumberColumn("Mín Cierres",  min_value=0, max_value=50, step=1),
        },
        key="requirements_editor",
    )

    if st.button("Guardar cobertura", type="primary"):
        try:
            replace_requirements(con, edited)
            st.success("Cobertura guardada ✔")
            st.rerun()
        except Exception as exc:
            st.error(f"Error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# GENERAR HORARIO
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Generar horario":
    st.markdown("# ⚙️ Generar Horario Semanal")

    if not has_business_data(con):
        st.warning("Sin datos — importa el Excel primero.")
        st.stop()

    emp      = employees_df(con)
    av       = availability_df(con)
    req      = requirements_df(con)
    schedule = schedules_df(con)

    if schedule.empty:
        st.warning("No hay horario base guardado. Importa el Excel primero.")
        st.stop()

    labels    = {schedule_label(row): int(row["id"]) for _, row in schedule.iterrows()}
    def_id    = latest_schedule_id(con)
    def_idx   = 0
    for i, (_, sid) in enumerate(labels.items()):
        if sid == def_id:
            def_idx = i
            break

    sel_label = st.selectbox("Horario base a reciclar", list(labels.keys()), index=def_idx)
    base_id   = labels[sel_label]

    c1, c2 = st.columns(2)
    with c1:
        week_start = st.date_input("Semana a generar (inicio)", value=next_week_start())
    with c2:
        notes = st.text_input("Notas", value="Generado desde la plataforma")

    # Solicitudes activas
    active_req = requests_for_week(con, week_start)
    st.markdown("### Solicitudes activas para esta semana")
    if not active_req.empty:
        st.dataframe(active_req, use_container_width=True, hide_index=True)
        # Detectar si alguna es en ventana válida
        wi = request_window_info(week_start)
        if not wi["is_open"]:
            st.markdown(
                """<div class="alert-warn">⏳ La ventana de solicitudes para esta semana ya cerró.
                Las solicitudes registradas de todas formas se aplicarán al generar.</div>""",
                unsafe_allow_html=True,
            )
    else:
        st.info("No hay solicitudes especiales para esta semana.")

    # Nuevos trabajadores sin turno base
    activos = emp[emp["Activo"] == True] if not emp.empty else pd.DataFrame()
    if not activos.empty:
        base_long_preview = schedule_long_df(con, base_id)
        workers_in_base   = set(base_long_preview["Trabajador"].unique()) if not base_long_preview.empty else set()
        new_workers       = activos[~activos["Trabajador"].isin(workers_in_base)]
        if not new_workers.empty:
            st.markdown(
                f"""<div class="alert-warn">
                🆕 <strong>{len(new_workers)} trabajador(es) nuevo(s)</strong> sin turno en el horario base —
                el motor los asignará según su disponibilidad y área:<br>
                {", ".join(new_workers["Trabajador"].tolist())}
                </div>""",
                unsafe_allow_html=True,
            )

    if st.button("🚀 Generar horario", type="primary"):
        try:
            base_long = schedule_long_df(con, base_id)
            out_wide, out_long, out_summary, warnings = generate_schedule(
                employees_df=emp,
                base_long_df=base_long,
                requirements_df=req,
                availability_df=av,
                requests_df=active_req,
                settings=settings,
            )
            st.session_state["generated_schedule"] = {
                "wide":       out_wide,
                "long":       out_long,
                "summary":    out_summary,
                "warnings":   warnings,
                "week_start": week_start,
                "notes":      notes,
            }
            st.success("✅ Horario generado — revísalo antes de guardar.")
        except Exception as exc:
            st.error(f"Error al generar: {exc}")

    if "generated_schedule" in st.session_state:
        gen = st.session_state["generated_schedule"]

        st.markdown("### Horario generado")
        st.dataframe(gen["wide"], use_container_width=True, hide_index=True)

        st.markdown("### Resumen por colaborador")
        st.dataframe(gen["summary"], use_container_width=True, hide_index=True)

        if gen["warnings"]:
            errors = [w for w in gen["warnings"] if w.startswith("🔴")]
            warns  = [w for w in gen["warnings"] if w.startswith(("⚠️", "🟡", "✅"))]
            others = [w for w in gen["warnings"] if not w.startswith(("🔴", "⚠️", "🟡", "✅"))]

            if errors:
                st.markdown("### ❌ Errores críticos")
                for e in errors:
                    st.markdown(f'<div class="alert-err">{e}</div>', unsafe_allow_html=True)
            if warns:
                st.markdown("### ⚠️ Avisos")
                for w in warns:
                    st.markdown(f'<div class="alert-warn">{w}</div>', unsafe_allow_html=True)
            if others:
                st.markdown("### ℹ️ Notas del motor")
                for n in others:
                    st.markdown(f'<div class="alert-ok">{n}</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="alert-ok">✅ Horario generado sin advertencias.</div>', unsafe_allow_html=True)

        excel_bytes = export_schedule_excel(gen["wide"], gen["long"], gen["summary"], gen["warnings"])

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "⬇️ Descargar Excel",
                data=excel_bytes,
                file_name=f"horario_{gen['week_start'].isoformat()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        with c2:
            if st.button("💾 Guardar en base de datos", type="primary"):
                try:
                    new_id = save_schedule(
                        con, gen["wide"], gen["long"], gen["warnings"],
                        week_start=gen["week_start"],
                        source="GENERADO",
                        notes=gen["notes"],
                        created_by=user["id"],
                        is_base=False,
                    )
                    st.success(f"Guardado como horario #{new_id} ✔")
                    st.session_state.pop("generated_schedule", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Error al guardar: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# HISTORIAL
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Historial":
    st.markdown("# 📚 Historial de Horarios")

    scheds = schedules_df(con)
    if scheds.empty:
        st.info("Aún no hay horarios guardados.")
        st.stop()

    labels = {schedule_label(row): int(row["id"]) for _, row in scheds.iterrows()}
    sel    = st.selectbox("Selecciona un horario", list(labels.keys()))
    sid    = labels[sel]

    long_df  = schedule_long_df(con, sid)
    wide_df  = long_to_wide(long_df) if not long_df.empty else pd.DataFrame()
    warns    = schedule_warnings(con, sid)
    emp      = employees_df(con)
    summary  = build_summary(long_df, emp[emp["Trabajador"].isin(long_df["Trabajador"].unique())]) \
               if not long_df.empty and not emp.empty else pd.DataFrame()

    tab1, tab2 = st.tabs(["Vista semanal", "Detalle por día"])
    with tab1:
        st.dataframe(wide_df, use_container_width=True, hide_index=True)
        st.markdown("### Resumen")
        st.dataframe(summary, use_container_width=True, hide_index=True)
    with tab2:
        if not long_df.empty:
            day_sel = st.selectbox("Día", DAYS)
            day_df  = long_df[long_df["Dia"] == day_sel].copy()
            st.dataframe(
                day_df[["Trabajador", "Area", "Turno", "Shift", "Horas Pagadas", "Cierre", "Apertura"]],
                use_container_width=True, hide_index=True,
            )

    if warns:
        st.markdown("### Advertencias guardadas")
        for w in warns:
            tag = "alert-err" if w.startswith("🔴") else ("alert-warn" if "⚠️" in w or "🟡" in w else "alert-ok")
            st.markdown(f'<div class="{tag}">{w}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="alert-ok">Sin advertencias para este horario.</div>', unsafe_allow_html=True)

    excel_bytes = export_schedule_excel(wide_df, long_df, summary, warns)
    st.download_button(
        "⬇️ Exportar a Excel",
        data=excel_bytes,
        file_name=f"horario_historico_{sid}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("### Todos los horarios")
    st.dataframe(scheds, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Configuración":
    st.markdown("# 🔧 Configuración General")
    st.write("Estos valores afectan la importación y generación de horarios.")

    st.markdown(
        f"""
        <div class="rule-box">
        <strong>Horas máximas:</strong>
        PT = <strong>{format_paid_hours(PT_MAX_MINUTES)} h</strong> (1140 min) |
        FT = <strong>{format_paid_hours(FT_MAX_MINUTES)} h</strong> (2880 min).<br>
        Ni un solo minuto más. El motor respeta este límite de forma estricta.
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("settings_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Contrato**")
            pt_hours  = st.number_input("Horas objetivo PT",  min_value=1.0,  max_value=19.0, value=float(settings["pt_hours"]),  step=0.5, help="Máximo permitido: 19.00 h")
            ft_hours  = st.number_input("Horas objetivo FT",  min_value=20.0, max_value=48.0, value=float(settings["ft_hours"]),  step=0.5, help="Máximo permitido: 48.00 h")
            ft_thresh = st.number_input("Inferir FT desde (h)", min_value=20.0, max_value=48.0, value=float(settings["ft_infer_threshold"]), step=1.0)
        with c2:
            st.markdown("**Descansos y refrigerio**")
            pt_rest    = st.number_input("Descansos mín PT", min_value=0, max_value=7, value=int(settings["pt_rest"]), step=1)
            ft_rest    = st.number_input("Descansos mín FT", min_value=0, max_value=7, value=int(settings["ft_rest"]), step=1)
            hour_tol   = st.number_input("Tolerancia (h)", min_value=0.0, max_value=2.0, value=float(settings["hour_tolerance"]), step=0.25)
            break_after = st.number_input("Refrigerio desde (h)", min_value=0.0, max_value=12.0, value=float(settings["break_after_hours"]), step=0.5)
            break_min  = st.number_input("Duración refrigerio (min)", min_value=0, max_value=120, value=int(settings["break_minutes"]), step=5)
        with c3:
            st.markdown("**Cierre**")
            close_from = st.number_input(
                "Cierre: termina desde la hora →",
                min_value=0, max_value=6, value=int(settings["close_from_hour"]), step=1,
                help="1 = 01:00 AM. Solo cuenta si el turno cruza medianoche.",
            )
            close_to   = st.number_input(
                "Cierre: termina hasta la hora →",
                min_value=0, max_value=6, value=int(settings["close_to_hour"]), step=1,
            )

        submitted = st.form_submit_button("Guardar configuración", type="primary")
        if submitted:
            # Validar que PT no supere 19h y FT no supere 48h
            if pt_hours > 19.0:
                st.error("El máximo para PT es 19.00 h.")
            elif ft_hours > 48.0:
                st.error("El máximo para FT es 48.00 h.")
            else:
                save_settings(con, {
                    "pt_hours":           pt_hours,
                    "ft_hours":           ft_hours,
                    "pt_rest":            pt_rest,
                    "ft_rest":            ft_rest,
                    "ft_infer_threshold": ft_thresh,
                    "hour_tolerance":     hour_tol,
                    "break_after_hours":  break_after,
                    "break_minutes":      break_min,
                    "close_from_hour":    close_from,
                    "close_to_hour":      close_to,
                })
                st.success("Configuración guardada ✔")
                st.rerun()

    st.divider()
    st.markdown("### Backup")
    if DB_PATH.exists():
        st.download_button(
            "⬇️ Descargar copia de seguridad (.db)",
            data=DB_PATH.read_bytes(),
            file_name="bembos_scheduler_backup.db",
        )


# ─────────────────────────────────────────────────────────────────────────────
# CUENTA
# ─────────────────────────────────────────────────────────────────────────────
elif page == "Cuenta":
    st.markdown(f"# 👤 Cuenta: {user['username']}")

    with st.form("change_password_form"):
        current = st.text_input("Contraseña actual", type="password")
        new     = st.text_input("Nueva contraseña",  type="password")
        new2    = st.text_input("Repetir nueva",      type="password")
        if st.form_submit_button("Cambiar contraseña"):
            if new != new2:
                st.error("Las contraseñas no coinciden.")
            else:
                try:
                    change_password(con, user["id"], current, new)
                    st.success("Contraseña actualizada ✔")
                except Exception as exc:
                    st.error(str(exc))

    st.divider()
    st.markdown("### Código de recuperación")
    st.write("Genera un nuevo código. Se muestra una sola vez — guárdalo de inmediato.")
    if st.button("Generar nuevo código"):
        code = rotate_recovery_code(con, user["id"])
        st.warning("Guarda este código ahora:")
        st.code(code)