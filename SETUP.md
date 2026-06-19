# RockPress Tracker — Guía de Setup en GitHub

## ¿Qué hace esto?

Un script Python que cada mañana:
1. Fetcha los sitios web de rock nacional español listados en `bbddMedios.md`
2. Envía el contenido a la API de Gemini para análisis y clasificación
3. Genera un informe diario en `informes/informe_YYYYMMDD.md`
4. Lo commitea automáticamente al repositorio

---

## Paso 1 — Crear el repositorio en GitHub

1. Ve a [github.com/new](https://github.com/new)
2. Crea un repo privado (recomendado): `cordobarock-tracker` o similar
3. Sube todos los ficheros de esta carpeta:

```
tu-repo/
├── bbddMedios.md              ← listado de medios (ya lo tienes)
├── rockpress_tracker.py       ← script principal
├── requirements.txt           ← dependencias Python
├── estrategia_fetch_optimizada.md
├── informes/                  ← carpeta donde se guardarán los informes
└── .github/
    └── workflows/
        └── rockpress_daily.yml ← workflow de GitHub Actions
```

Para subir desde terminal:
```bash
cd /ruta/a/CordobaRock
git init
git add .
git commit -m "Initial commit – RockPress Tracker"
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

---

## Paso 2 — Obtener clave de API de Gemini (gratis)

1. Ve a [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
2. Haz clic en **"Create API Key"**
3. Copia la clave generada (empieza por `AIzaSy...`)

> El tier gratuito de Gemini Flash permite ~1.500 llamadas/día y 1M tokens/minuto.
> Para uso diario de este tracker es más que suficiente, sin coste.

---

## Paso 3 — Añadir la clave como Secret en GitHub

1. En tu repositorio, ve a **Settings → Secrets and variables → Actions**
2. Haz clic en **"New repository secret"**
3. Nombre: `GEMINI_API_KEY`
4. Valor: la clave que copiaste en el paso anterior
5. Guardar

---

## Paso 4 — Activar GitHub Actions

GitHub Actions se activa automáticamente al detectar el fichero `.github/workflows/rockpress_daily.yml`.

El cron está configurado para las **8:00 UTC** (9:00h España en invierno, 10:00h en verano).

Para cambiar la hora, edita esta línea en el workflow:
```yaml
- cron: '0 8 * * *'   # minuto hora * * * (UTC)
```

Ejemplos:
- `'0 7 * * *'` → 8:00 CET / 9:00 CEST
- `'0 6 * * 1-5'` → Solo días laborables

---

## Paso 5 — Ejecución manual (para probar)

1. Ve a la pestaña **Actions** de tu repositorio
2. Selecciona **"RockPress Tracker – Informe Diario"**
3. Haz clic en **"Run workflow"**
4. Opcional: cambia el modelo Gemini o la ventana de días
5. Pulsa el botón verde **"Run workflow"**

El informe aparecerá en `informes/informe_YYYYMMDD.md` en el repo.

---

## Ejecución local (para desarrollo/pruebas)

```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar la clave (o exportarla en tu .zshrc/.bashrc)
export GEMINI_API_KEY="AIzaSy..."

# Ejecutar
python rockpress_tracker.py
```

---

## Variables de entorno opcionales

| Variable | Por defecto | Descripción |
|---|---|---|
| `GEMINI_API_KEY` | — | **Obligatoria**. Clave de la API de Gemini |
| `GEMINI_MODEL` | `gemini-1.5-flash` | Modelo a usar. Alternativas: `gemini-1.5-pro`, `gemini-2.0-flash` |
| `DATE_WINDOW_DAYS` | `3` | Días hacia atrás para buscar noticias |

---

## Añadir o quitar medios

Edita `bbddMedios.md` y añade filas a la tabla:

```
| Nombre del Medio | https://url-del-medio.com | https://url-del-medio.com/feed/ |
```

Si es un sitio WordPress, añade también la URL base en el diccionario `WP_ARCHIVE_SITES` 
dentro de `rockpress_tracker.py` para que use archive pages (más eficiente).

---

## Estructura del informe generado

```
informes/
├── informe_20260619.md
├── informe_20260620.md
└── ...
```

Cada informe sigue el formato estándar del RockPress Tracker con secciones
de alta/media-alta/media relevancia, resumen general y tabla de fuentes.

---

## Coste estimado

Con `gemini-1.5-flash` y el tier gratuito:
- **0 €/mes** para uso diario normal
- El tier de pago cuesta ~$0.001-0.003 por ejecución completa

---

*Última actualización: junio 2026*
