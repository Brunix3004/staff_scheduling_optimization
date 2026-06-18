# Plataforma de Gestión de Horarios

Herramienta local en Streamlit para administrar colaboradores, disponibilidad, solicitudes especiales y generación histórica de horarios para restaurante.

## Qué incluye

- Login de administradores con sesión que se mantiene al recargar la pestaña.
- Registro de administradores con código de registro.
- Recuperación de contraseña mediante código de recuperación.
- Cambio de contraseña desde la cuenta.
- Base de datos SQLite persistente.
- Carga inicial desde Excel.
- Gestión de colaboradores activos/inactivos.
- Gestión de disponibilidad semanal.
- Gestión de solicitudes especiales por fecha.
- Configuración de cobertura requerida por día y área.
- Generación de horarios basada en el último horario guardado.
- Historial de horarios generados/importados.
- Exportación a Excel.
- Backup descargable de la base de datos.

## Formato esperado del Excel inicial

El archivo puede tener estas hojas:

### `colaboradores`

Columnas recomendadas:

- `COLABORADOR`
- `AREA` (`SERVICIO` o `PRODUCCION`)
- `TURNO` (`PT` o `FT`, puede estar vacío y se infiere desde el horario)
- `ESTADO` (`ACTIVO` o `INACTIVO`)
- `COMENTARIO`

### `disponibilidad`

Columnas recomendadas:

- `COLABORADOR`
- `DIA`
- `DESDE`
- `HASTA`
- `OBSERVACION`

Si esta hoja está vacía, la plataforma crea una disponibilidad inicial usando el `horario_base`.

### `solicitudes`

Columnas recomendadas:

- `COLABORADOR`
- `FECHA`
- `TIPO_SOLICITUD`
- `COMENTARIO`

Tipos soportados desde la app:

- `NO_TRABAJA`
- `SOLICITA_DESCANSO`
- `NO_DISPONIBLE`
- `FERIADO`

### `horario_base`

Columnas esperadas:

- `INICIO_SEMANA`
- `FIN_SEMANA`
- `COLABORADOR`
- `AREA`
- `TURNO`
- `LUNES`
- `MARTES`
- `MIERCOLES`
- `JUEVES`
- `VIERNES`
- `SABADO`
- `DOMINGO`

Los turnos pueden estar como `08:15 - 17:00`, `16:15 - 01:00`, `OFF`, `NULL` o vacío.

Para la cobertura de cierre, la plataforma cuenta como cierre únicamente los turnos que cruzan medianoche y terminan a la `01:00` o después.

## Instalación

Desde la carpeta del proyecto:

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Primer uso

1. Abre la app.
2. Crea el primer administrador.
3. Guarda el código de recuperación que se mostrará una sola vez.
4. Haz clic en **Ya copié el código, ir a iniciar sesión**.
5. Inicia sesión con el usuario y contraseña que acabas de crear.
6. Ve a **Carga inicial / Excel**.
7. Sube el archivo Excel inicial.
8. Importa el archivo a la base de datos.
9. Desde ese momento ya puedes trabajar desde la plataforma sin volver a subir el Excel cada vez.

## Código de registro de nuevos admins

Por defecto es:

```text
bembos-admin-2026
```

Para cambiarlo, crea un archivo:

```text
.streamlit/secrets.toml
```

con este contenido:

```toml
ADMIN_REGISTRATION_CODE = "tu-codigo-seguro"
```

## Sesión

Al iniciar sesión, la app genera un token temporal en la URL para que la sesión no se pierda si recargas la página. Usa **Cerrar sesión** para invalidarlo.

## Base de datos

La base se crea automáticamente en:

```text
data/bembos_scheduler.db
```

No borres este archivo si quieres conservar colaboradores, disponibilidad, solicitudes e historial.

## Recomendación importante

Para dar de baja a un colaborador, no lo borres. Cámbialo a `Activo = false`. Así la plataforma conserva su historial.

## Actualización del motor v4

Esta versión mantiene la interfaz de la v2 y actualiza el motor de generación:

- Una solicitud `NO_TRABAJA`, `NO DISPONIBLE`, `DESCANSO` o similar ya no solo elimina el turno: ahora dispara una reparación del horario.
- El sistema intenta completar las horas objetivo de cada colaborador: PT = 19 h y FT = 48 h, según configuración.
- Si un colaborador que cerraba pide descanso, se busca otro colaborador de la misma área disponible para cubrir ese cierre.
- Si el horario base no cumple las horas objetivo, el nuevo horario intenta corregirlo usando la disponibilidad registrada.
- Si no existe disponibilidad suficiente para completar horas o cubrir cierres, se muestra una validación clara.

La calidad del resultado depende de que la hoja `disponibilidad` esté completa. Si solo se importa `horario_base`, la app puede reciclar patrones, pero no puede inventar disponibilidad real en días donde el colaborador no informó que puede trabajar.
