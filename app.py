from __future__ import annotations

from datetime import date, timedelta
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
from scheduler_core import DAYS, build_summary, export_schedule_excel, generate_schedule, long_to_wide

st.set_page_config(page_title="Gestión de Horarios", page_icon="📅", layout="wide")

APP_DIR = Path(__file__).resolve().parent
IMPORT_DIR = APP_DIR / "data" / "imports"
IMPORT_DIR.mkdir(parents=True, exist_ok=True)


def get_con():
    con = get_connection()
    init_db(con)
    return con


con = get_con()
cleanup_expired_sessions(con)


# ----------------------------- UI helpers -----------------------------

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


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .main .block-container {padding-top: 1.4rem; max-width: 1280px;}
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #fff7ed 0%, #ffffff 55%, #fff 100%);
            border-right: 1px solid #f0d9c0;
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {margin-bottom: .35rem;}
        .brand-card {
            background: linear-gradient(135deg, #d71920 0%, #ffb000 100%);
            color: white;
            border-radius: 18px;
            padding: 16px 14px;
            margin-bottom: 14px;
            box-shadow: 0 8px 18px rgba(215,25,32,.18);
        }
        .brand-title {font-size: 1.25rem; font-weight: 800; line-height: 1.1;}
        .brand-subtitle {font-size: .82rem; opacity: .92; margin-top: 5px;}
        .sidebar-user {
            background: #ffffff;
            border: 1px solid #f0d9c0;
            border-radius: 14px;
            padding: 10px 12px;
            margin-bottom: 12px;
        }
        div[data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #eeeeee;
            padding: 14px 16px;
            border-radius: 16px;
            box-shadow: 0 3px 12px rgba(0,0,0,.035);
        }
        .section-card {
            background: #ffffff;
            border: 1px solid #eeeeee;
            border-radius: 16px;
            padding: 16px 18px;
            margin: 10px 0 18px 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def require_auth() -> dict:
    if "auth_user" in st.session_state and st.session_state["auth_user"]:
        return st.session_state["auth_user"]

    url_token = get_session_token_from_url()
    if url_token:
        restored_user = user_from_session(con, url_token)
        if restored_user:
            st.session_state["auth_user"] = restored_user
            st.session_state["session_token"] = url_token
            return restored_user
        clear_session_token_from_url()

    st.title("🔐 Acceso de administrador")
    st.caption("Inicia sesión para gestionar colaboradores, disponibilidad, solicitudes y horarios.")

    first_user = count_users(con) == 0
    if first_user:
        st.info("Primera ejecución: crea el primer administrador. Guarda bien el código de recuperación que se generará.")
        tabs = st.tabs(["Crear primer admin", "Recuperar contraseña"])
    else:
        tabs = st.tabs(["Iniciar sesión", "Registrar admin", "Recuperar contraseña"])

    def login_tab():
        with st.form("login_form"):
            username = st.text_input("Usuario o correo")
            password = st.text_input("Contraseña", type="password")
            submitted = st.form_submit_button("Iniciar sesión", type="primary")
        if submitted:
            user = authenticate_user(con, username, password)
            if user:
                token = create_login_session(con, user["id"], hours=12)
                st.session_state["auth_user"] = user
                st.session_state["session_token"] = token
                set_session_token_in_url(token)
                st.success("Sesión iniciada.")
                st.rerun()
            else:
                st.error("Usuario/correo o contraseña incorrectos.")

    def register_tab(first: bool = False):
        with st.form("register_form"):
            username = st.text_input("Usuario", key="reg_username")
            email = st.text_input("Correo", key="reg_email")
            password = st.text_input("Contraseña", type="password", key="reg_pass")
            password2 = st.text_input("Repetir contraseña", type="password", key="reg_pass2")
            code = ""
            if not first:
                code = st.text_input("Código de registro admin", type="password")
                st.caption("Por defecto local: bembos-admin-2026. Puedes cambiarlo en .streamlit/secrets.toml o variable de entorno.")
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
                st.session_state["admin_created_ok"] = True
                st.success("Administrador creado correctamente.")
            except Exception as exc:
                st.error(f"No se pudo crear el usuario: {exc}")

        if st.session_state.get("admin_created_ok") and st.session_state.get("last_recovery_code"):
            st.warning("Guarda este código de recuperación. No se volverá a mostrar si cierras o recargas la página:")
            st.code(st.session_state["last_recovery_code"])
            st.info("Después de copiarlo, continúa al inicio de sesión.")
            if st.button("Ya copié el código, ir a iniciar sesión", type="primary", key="go_to_login_after_register"):
                st.session_state.pop("last_recovery_code", None)
                st.session_state.pop("admin_created_ok", None)
                st.rerun()

    def recover_tab():
        with st.form("recover_form"):
            username = st.text_input("Usuario o correo", key="rec_user")
            recovery = st.text_input("Código de recuperación", key="rec_code")
            new_password = st.text_input("Nueva contraseña", type="password", key="rec_pass")
            submitted = st.form_submit_button("Restablecer contraseña")
        if submitted:
            try:
                reset_password_with_code(con, username, recovery, new_password)
                st.success("Contraseña restablecida. Ya puedes iniciar sesión.")
            except Exception as exc:
                st.error(str(exc))

    if first_user:
        with tabs[0]:
            register_tab(first=True)
        with tabs[1]:
            recover_tab()
    else:
        with tabs[0]:
            login_tab()
        with tabs[1]:
            register_tab(first=False)
        with tabs[2]:
            recover_tab()

    st.stop()


inject_styles()
user = require_auth()
settings = get_settings(con)


# ----------------------------- Sidebar -----------------------------

PAGE_LABELS = {
    "Inicio": "🏠 Inicio",
    "Carga inicial / Excel": "📥 Carga inicial",
    "Colaboradores": "👥 Colaboradores",
    "Disponibilidad": "🗓️ Disponibilidad",
    "Solicitudes especiales": "📝 Solicitudes",
    "Cobertura requerida": "🎯 Cobertura",
    "Generar horario": "⚙️ Generar horario",
    "Historial y exportación": "📚 Historial / Exportar",
    "Configuración": "🔧 Configuración",
    "Cuenta": "👤 Cuenta",
}
LABEL_TO_PAGE = {v: k for k, v in PAGE_LABELS.items()}

st.sidebar.markdown(
    """
    <div class="brand-card">
        <div class="brand-title">🍔 Bembos Scheduler</div>
        <div class="brand-subtitle">Gestión semanal de horarios</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.markdown(
    f"""
    <div class="sidebar-user">
        <b>Administrador</b><br>
        <span>{user['username']}</span>
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
if st.sidebar.button("🚪 Cerrar sesión", use_container_width=True):
    token = st.session_state.get("session_token") or get_session_token_from_url()
    revoke_login_session(con, token)
    st.session_state.pop("auth_user", None)
    st.session_state.pop("session_token", None)
    clear_session_token_from_url()
    st.rerun()


# ----------------------------- Pages -----------------------------

st.title("📅 Gestión de horarios")

if page == "Inicio":
    has_data = has_business_data(con)
    emp = employees_df(con)
    sched = schedules_df(con)
    req = requests_df(con)

    st.subheader("Panel principal")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Colaboradores", len(emp))
    c2.metric("Activos", int(emp["Activo"].sum()) if not emp.empty else 0)
    c3.metric("Horarios guardados", len(sched))
    c4.metric("Solicitudes", len(req))

    if not has_data:
        st.warning("Todavía no hay datos cargados. Empieza en 'Carga inicial'.")
    else:
        st.success("Información cargada correctamente. Puedes trabajar desde la plataforma sin volver a importar el Excel.")

    if not emp.empty:
        st.subheader("Colaboradores activos por área")
        active = emp[emp["Activo"] == True]
        if not active.empty:
            st.dataframe(active.groupby(["Area", "Turno"], as_index=False).size(), use_container_width=True, hide_index=True)
        else:
            st.info("No hay colaboradores activos registrados.")

elif page == "Carga inicial / Excel":
    st.subheader("Carga inicial desde Excel")
    st.write("Usa esta sección solo para la primera carga o para reimportar datos históricos. Después podrás trabajar directamente desde la plataforma.")

    uploaded = st.file_uploader("Sube el Excel con hojas: colaboradores, disponibilidad, solicitudes, horario_base", type=["xlsx", "xls"])
    overwrite = st.checkbox("Sobrescribir datos actuales", value=not has_business_data(con), help="Si está activo, borra colaboradores, disponibilidad, solicitudes y horarios guardados antes de importar.")

    if uploaded is not None:
        if st.button("Importar Excel a la base de datos", type="primary"):
            try:
                path = save_uploaded_file(uploaded)
                result = import_initial_workbook(con, path, uploaded.name, settings, created_by=user["id"], overwrite=overwrite)
                st.success("Excel importado y guardado en SQLite.")
                st.json(result)
            except Exception as exc:
                st.error(f"No se pudo importar el archivo: {exc}")

    st.divider()
    st.subheader("Estado actual")
    st.write("Estos datos ya están guardados en la base de datos:")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Colaboradores", len(employees_df(con)))
    c2.metric("Disponibilidad", len(availability_df(con)))
    c3.metric("Solicitudes", len(requests_df(con)))
    c4.metric("Horarios", len(schedules_df(con)))

elif page == "Colaboradores":
    st.subheader("Gestión de colaboradores")
    st.write("Agrega, edita o da de baja colaboradores. Para dar de baja, desmarca Activo; no lo borres si quieres mantener historial.")
    emp = employees_df(con)
    if emp.empty:
        emp = pd.DataFrame(columns=["Trabajador", "Area", "Turno", "Activo", "Max Horas", "Min Descansos", "Horas Semana Base", "Dias Trabajados Base", "Descansos Base", "Comentario"])

    edited = st.data_editor(
        emp,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Area": st.column_config.SelectboxColumn("Area", options=["SERVICIO", "PRODUCCION"], required=True),
            "Turno": st.column_config.SelectboxColumn("Turno", options=["PT", "FT"], required=True),
            "Activo": st.column_config.CheckboxColumn("Activo", default=True),
            "Max Horas": st.column_config.NumberColumn("Max Horas", min_value=0.0, max_value=70.0, step=0.5),
            "Min Descansos": st.column_config.NumberColumn("Min Descansos", min_value=0, max_value=7, step=1),
        },
        key="employees_editor",
    )

    if st.button("Guardar colaboradores", type="primary"):
        try:
            replace_employees(con, edited)
            st.success("Colaboradores guardados en la base de datos.")
            st.rerun()
        except Exception as exc:
            st.error(f"No se pudo guardar: {exc}")

elif page == "Disponibilidad":
    st.subheader("Disponibilidad semanal")
    st.write("Aquí se guarda lo que cada colaborador puede trabajar, no necesariamente lo que se le asignará.")
    emp = employees_df(con)
    av = availability_df(con)
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
            "Dia": st.column_config.SelectboxColumn("Dia", options=DAYS, required=True),
            "Hora Inicio": st.column_config.TextColumn("Hora Inicio", help="Ejemplo: 17:00"),
            "Hora Fin": st.column_config.TextColumn("Hora Fin", help="Ejemplo: 23:00 o 01:00"),
            "Disponible": st.column_config.CheckboxColumn("Disponible", default=True),
        },
        key="availability_editor",
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Guardar disponibilidad", type="primary"):
            try:
                replace_availability(con, edited)
                st.success("Disponibilidad guardada.")
                st.rerun()
            except Exception as exc:
                st.error(f"No se pudo guardar: {exc}")
    with col2:
        st.info("Si alguien tiene dos bloques en un mismo día, puedes crear dos filas para el mismo colaborador y día.")

elif page == "Solicitudes especiales":
    st.subheader("Solicitudes especiales")
    st.write("Registra cambios puntuales con el mismo formato de la hoja `solicitudes`: COLABORADOR, FECHA, TIPO_SOLICITUD y COMENTARIO.")
    emp = employees_df(con)
    reqs = requests_df(con)
    if reqs.empty:
        reqs = pd.DataFrame({
            "Trabajador": pd.Series(dtype="object"),
            "Fecha": pd.Series(dtype="datetime64[ns]"),
            "Tipo Solicitud": pd.Series(dtype="object"),
            "Comentario": pd.Series(dtype="object"),
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
            "Trabajador": st.column_config.SelectboxColumn("COLABORADOR", options=names, required=True),
            "Fecha": st.column_config.DateColumn("FECHA", format="YYYY-MM-DD", required=True),
            "Tipo Solicitud": st.column_config.SelectboxColumn(
                "TIPO_SOLICITUD",
                options=["NO_TRABAJA", "SOLICITA_DESCANSO", "NO_DISPONIBLE", "FERIADO"],
                required=True,
            ),
            "Comentario": st.column_config.TextColumn("COMENTARIO"),
        },
        key="requests_editor",
    )

    if st.button("Guardar solicitudes", type="primary"):
        try:
            edited = edited[["Trabajador", "Fecha", "Tipo Solicitud", "Comentario"]].copy()
            replace_requests(con, edited)
            st.success("Solicitudes guardadas.")
            st.rerun()
        except Exception as exc:
            st.error(f"No se pudo guardar: {exc}")

elif page == "Cobertura requerida":
    st.subheader("Cobertura requerida por día y área")
    st.write("La carga inicial detecta cierres solo cuando el turno cruza la medianoche y termina a la 01:00 o después. Puedes ajustar los mínimos manualmente si la operación lo requiere.")
    req = requirements_df(con)
    if req.empty:
        rows = []
        for d in DAYS:
            for area in ["SERVICIO", "PRODUCCION"]:
                rows.append({"Dia": d, "Area": area, "Min Personas": 0, "Min Cierres": 0})
        req = pd.DataFrame(rows)

    edited = st.data_editor(
        req,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Dia": st.column_config.SelectboxColumn("Dia", options=DAYS, required=True),
            "Area": st.column_config.SelectboxColumn("Area", options=["SERVICIO", "PRODUCCION"], required=True),
            "Min Personas": st.column_config.NumberColumn("Min Personas", min_value=0, max_value=50, step=1),
            "Min Cierres": st.column_config.NumberColumn("Min Cierres", min_value=0, max_value=50, step=1),
        },
        key="requirements_editor",
    )

    if st.button("Guardar cobertura", type="primary"):
        try:
            replace_requirements(con, edited)
            st.success("Cobertura guardada.")
            st.rerun()
        except Exception as exc:
            st.error(f"No se pudo guardar: {exc}")

elif page == "Generar horario":
    st.subheader("Generar nuevo horario semanal")
    if not has_business_data(con):
        st.warning("Primero importa el Excel inicial.")
        st.stop()

    emp = employees_df(con)
    av = availability_df(con)
    req = requirements_df(con)
    schedules = schedules_df(con)
    if schedules.empty:
        st.warning("No hay horario base guardado. Importa primero un Excel con horario_base.")
        st.stop()

    labels = {schedule_label(row): int(row["id"]) for _, row in schedules.iterrows()}
    default_id = latest_schedule_id(con)
    default_index = 0
    for i, (_, sid) in enumerate(labels.items()):
        if sid == default_id:
            default_index = i
            break
    selected_label = st.selectbox("Horario base para reciclar", list(labels.keys()), index=default_index)
    base_id = labels[selected_label]

    c1, c2 = st.columns(2)
    with c1:
        week_start = st.date_input("Inicio de semana a generar", value=default_week_start())
    with c2:
        notes = st.text_input("Notas del horario", value="Generado desde la plataforma")

    active_requests = requests_for_week(con, week_start)
    if not active_requests.empty:
        st.info("Solicitudes activas que afectarán esta semana:")
        st.dataframe(active_requests, use_container_width=True, hide_index=True)
    else:
        st.info("No hay solicitudes especiales activas para esa semana.")

    if st.button("Generar horario", type="primary"):
        try:
            base_long = schedule_long_df(con, base_id)
            out_wide, out_long, out_summary, warnings = generate_schedule(
                employees_df=emp,
                base_long_df=base_long,
                requirements_df=req,
                availability_df=av,
                requests_df=active_requests,
                settings=settings,
            )
            st.session_state["generated_schedule"] = {
                "wide": out_wide,
                "long": out_long,
                "summary": out_summary,
                "warnings": warnings,
                "week_start": week_start,
                "notes": notes,
            }
            st.success("Horario generado. Revísalo antes de guardarlo.")
        except Exception as exc:
            st.error(f"No se pudo generar el horario: {exc}")

    if "generated_schedule" in st.session_state:
        gen = st.session_state["generated_schedule"]
        st.subheader("Horario generado")
        st.dataframe(gen["wide"], use_container_width=True, hide_index=True)

        st.subheader("Resumen")
        st.dataframe(gen["summary"], use_container_width=True, hide_index=True)

        if gen["warnings"]:
            st.warning("Validaciones y movimientos aplicados:")
            for w in gen["warnings"]:
                st.write(f"- {w}")
        else:
            st.success("Horario generado sin advertencias.")

        excel_bytes = export_schedule_excel(gen["wide"], gen["long"], gen["summary"], gen["warnings"])
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Descargar Excel generado",
                data=excel_bytes,
                file_name=f"horario_generado_{gen['week_start'].isoformat()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        with c2:
            if st.button("Guardar horario en la base de datos", type="primary"):
                try:
                    new_id = save_schedule(
                        con,
                        gen["wide"],
                        gen["long"],
                        gen["warnings"],
                        week_start=gen["week_start"],
                        source="GENERADO",
                        notes=gen["notes"],
                        created_by=user["id"],
                        is_base=False,
                    )
                    st.success(f"Horario guardado históricamente con ID #{new_id}.")
                except Exception as exc:
                    st.error(f"No se pudo guardar el horario: {exc}")

elif page == "Historial y exportación":
    st.subheader("Historial de horarios")
    schedules = schedules_df(con)
    if schedules.empty:
        st.info("Aún no hay horarios guardados.")
        st.stop()

    labels = {schedule_label(row): int(row["id"]) for _, row in schedules.iterrows()}
    selected_label = st.selectbox("Selecciona un horario", list(labels.keys()))
    sid = labels[selected_label]
    long_df = schedule_long_df(con, sid)
    wide_df = long_to_wide(long_df) if not long_df.empty else pd.DataFrame()
    warnings = schedule_warnings(con, sid)
    emp = employees_df(con)
    summary = build_summary(long_df, emp[emp["Trabajador"].isin(long_df["Trabajador"].unique())]) if not long_df.empty and not emp.empty else pd.DataFrame()

    st.dataframe(wide_df, use_container_width=True, hide_index=True)
    st.subheader("Resumen")
    st.dataframe(summary, use_container_width=True, hide_index=True)

    if warnings:
        st.warning("Validaciones guardadas:")
        for w in warnings:
            st.write(f"- {w}")
    else:
        st.success("Este horario no tiene advertencias guardadas.")

    excel_bytes = export_schedule_excel(wide_df, long_df, summary, warnings)
    st.download_button(
        "Exportar este horario a Excel",
        data=excel_bytes,
        file_name=f"horario_historico_{sid}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.subheader("Todos los horarios guardados")
    st.dataframe(schedules, use_container_width=True, hide_index=True)

elif page == "Configuración":
    st.subheader("Reglas generales")
    st.write("Estos valores se guardan en la base de datos y afectan la importación/generación de horarios.")
    with st.form("settings_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            pt_hours = st.number_input("Horas PT", min_value=1.0, max_value=40.0, value=float(settings["pt_hours"]), step=0.5)
            ft_hours = st.number_input("Horas FT", min_value=20.0, max_value=70.0, value=float(settings["ft_hours"]), step=0.5)
            ft_threshold = st.number_input("Inferir FT desde horas", min_value=20.0, max_value=70.0, value=float(settings["ft_infer_threshold"]), step=1.0)
        with c2:
            pt_rest = st.number_input("Descansos mínimos PT", min_value=0, max_value=7, value=int(settings["pt_rest"]), step=1)
            ft_rest = st.number_input("Descansos mínimos FT", min_value=0, max_value=7, value=int(settings["ft_rest"]), step=1)
            hour_tolerance = st.number_input("Tolerancia de horas", min_value=0.0, max_value=5.0, value=float(settings["hour_tolerance"]), step=0.25)
        with c3:
            break_after = st.number_input("Refrigerio desde horas", min_value=0.0, max_value=12.0, value=float(settings["break_after_hours"]), step=0.5)
            break_minutes = st.number_input("Minutos de refrigerio", min_value=0, max_value=120, value=int(settings["break_minutes"]), step=5)
            close_from = st.number_input("Cierre si termina desde hora", min_value=0, max_value=23, value=int(settings["close_from_hour"]), step=1, help="Para Bembos: 1 significa 01:00. Solo cuenta si el turno cruza medianoche.")
        submitted = st.form_submit_button("Guardar configuración", type="primary")
    if submitted:
        save_settings(con, {
            "pt_hours": pt_hours,
            "ft_hours": ft_hours,
            "pt_rest": pt_rest,
            "ft_rest": ft_rest,
            "ft_infer_threshold": ft_threshold,
            "hour_tolerance": hour_tolerance,
            "break_after_hours": break_after,
            "break_minutes": break_minutes,
            "close_from_hour": close_from,
            "close_to_hour": settings["close_to_hour"],
        })
        st.success("Configuración guardada.")
        st.rerun()

    st.divider()
    st.subheader("Backup local")
    if DB_PATH.exists():
        st.download_button("Descargar backup de la base de datos SQLite", data=DB_PATH.read_bytes(), file_name="bembos_scheduler_backup.db")

elif page == "Cuenta":
    st.subheader("Cuenta de administrador")
    st.write(f"Usuario actual: **{user['username']}**")
    with st.form("change_password_form"):
        current = st.text_input("Contraseña actual", type="password")
        new = st.text_input("Nueva contraseña", type="password")
        new2 = st.text_input("Repetir nueva contraseña", type="password")
        submitted = st.form_submit_button("Cambiar contraseña")
    if submitted:
        if new != new2:
            st.error("Las contraseñas no coinciden.")
        else:
            try:
                change_password(con, user["id"], current, new)
                st.success("Contraseña actualizada.")
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("Código de recuperación")
    st.write("Puedes generar un nuevo código. Guárdalo; solo se muestra una vez.")
    if st.button("Generar nuevo código de recuperación"):
        code = rotate_recovery_code(con, user["id"])
        st.warning("Guarda este código nuevo:")
        st.code(code)

