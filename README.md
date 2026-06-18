# 🍔 Bembos Scheduler 

Plataforma local en Streamlit para gestionar horarios semanales del personal de Bembos.

## Instalación

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Primer uso

1. Abre la app → crea el primer administrador.
2. Guarda el **código de recuperación** que aparece una sola vez.
3. Inicia sesión → ve a **Carga inicial** → sube el Excel.
4. Desde ese momento trabajas directo en la plataforma.

---

## Reglas de negocio implementadas

### Horas máximas (límite estricto)
| Tipo | Horas | Minutos |
|------|-------|---------|
| PT (Part Time) | **19:00 h** | 1140 min |
| FT (Full Time) | **48:00 h** | 2880 min |

No se puede superar ni un minuto. Las horas se muestran en formato `HH:MM`.

### Formato de hora
Siempre `HH:MM`. El valor `1:00` o `01:00` = **1 AM** (no 1 minuto).

### Apertura
Turnos que empiezan a las **07:00** u **08:00**.

### Cierre
Turnos que **cruzan medianoche** y terminan a las **01:00** o después.

**Mínimos por noche de cierre:**
- Producción: **3 personas**
- Servicio: **2 personas**

### Solicitudes de descanso
Los horarios se publican el **domingo** para la semana siguiente.
Las solicitudes se aceptan desde ese domingo hasta el **martes a las 12:00**.

Al aprobar una solicitud:
- Se libera ese día al trabajador.
- El motor intenta reubicar sus horas en otro día disponible (mismas horas, respetando disponibilidad).
- Si era cierre o apertura, se busca un sustituto de la misma área.

### Nuevo trabajador
Se agrega en **Colaboradores**, se llena su **Disponibilidad** y el siguiente horario
generado lo incluye automáticamente, asignándolo según área y disponibilidad.

### Baja de trabajador
Marcar como **Inactivo** (no borrar). El motor no lo genera.
Si cubría cierre o apertura, busca un sustituto automáticamente.

---

## Formato del Excel inicial

### Hoja `colaboradores`
`COLABORADOR | AREA | TURNO | ESTADO | COMENTARIO`

### Hoja `disponibilidad`
`COLABORADOR | DIA | DESDE | HASTA | OBSERVACION`

### Hoja `solicitudes`
`COLABORADOR | FECHA | TIPO_SOLICITUD | COMENTARIO`

Tipos: `NO_TRABAJA`, `SOLICITA_DESCANSO`, `NO_DISPONIBLE`, `FERIADO`

### Hoja `horario_base`
`INICIO_SEMANA | FIN_SEMANA | COLABORADOR | AREA | TURNO | LUNES...DOMINGO`

Turnos: `08:15 - 17:00`, `16:15 - 01:00`, `OFF` o vacío.

---

## Código de registro de admins

Por defecto: `bembos-admin-2026`

Para cambiarlo, crea `.streamlit/secrets.toml`:
```toml
ADMIN_REGISTRATION_CODE = "tu-codigo-seguro"
```

## Base de datos

Se crea automáticamente en `data/bembos_scheduler.db`. No la borres.
Usa **Configuración → Backup** para descargar una copia.
