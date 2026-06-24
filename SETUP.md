# RockPress Tracker — Guía de Setup en GitHub

## ¿Qué hace esto?

Un script Python que cada mañana:
1. Fetcha los sitios web de rock nacional español listados en `bbddMedios.md`
2. Filtra artículos ya reportados usando la memoria persistente (`seen_articles.json`)
3. Envía el contenido a la API de Gemini para análisis y clasificación
4. Genera un informe diario en `informes/informe_YYYYMMDD.md`
5. Actualiza la memoria y commitea informe + memoria al repositorio
6. Envía el informe por email vía Resend

---

## Paso 1 — Crear el repositorio en GitHub

1. Ve a [github.com/new](https://github.com/new)
2. Crea un repo privado (recomendado): `cordobarock-tracker` o similar
3. Sube todos los ficheros de esta carpeta:

```
tu-repo/
├── bbddMedios.md                    ← listado de medios
├── rockpress_tracker.py             ← script principal
├── requirements.txt                 ← dependencias Python
├── estrategia_fetch_optimizada.md
├── informes/                        ← informes + memoria (auto-generados)
│   ├── informe_YYYYMMDD.md
│   └── seen_articles.json           ← memoria de artículos ya reportados
└── .github/
    └── workflows/
        └── rockpress_daily.yml      ← workflow de GitHub Actions
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

## Paso 3 — Obtener clave de API de Resend (email, gratis)

1. Crea una cuenta en [resend.com](https://resend.com) (gratuito)
2. Ve a **API Keys → Create API Key**
3. Copia la clave generada (empieza por `re_...`)

**Nota sobre el dominio remitente:**
- Para **pruebas**: puedes usar `onboarding@resend.dev` como `EMAIL_FROM`. Solo puede enviar al email con el que te registraste en Resend.
- Para **producción**: verifica tu propio dominio en Resend (DNS → Settings → Domains) y usa `noreply@tudominio.com`.

> Tier gratuito: 3.000 emails/mes, 100/día. Suficiente para uso personal.

---

## Paso 4 — Configurar Secrets y Variables en GitHub

Ve a tu repositorio → **Settings → Secrets and variables → Actions**

### Secrets (valores sensibles — nunca visibles en logs)

| Secret | Valor | Obligatorio |
|---|---|---|
| `GEMINI_API_KEY` | Tu clave de Google AI Studio (`AIzaSy...`) | Depende (1) |
| `OPENAI_API_KEY` | Tu clave de OpenAI (`sk-...`) | Depende (1) |
| `ANTHROPIC_API_KEY`| Tu clave de Anthropic (`sk-ant-...`) | Depende (1) |
| `RESEND_API_KEY` | Tu clave de Resend (`re_...`) | Solo si quieres email |

*(1) Es obligatorio configurar **al menos una** de las tres API Keys según el modelo que elijas.*

Para añadir cada secret: **New repository secret** → introduce nombre y valor → **Add secret**

### Variables (valores no sensibles — visibles en logs)

Ve a la pestaña **Variables** (misma sección):

| Variable | Valor de ejemplo | Obligatorio |
|---|---|---|
| `EMAIL_TO` | `jahipe@gmail.com` | Solo si quieres email |
| `EMAIL_FROM` | `RockPress <noreply@tudominio.com>` | No (hay valor por defecto) |

Para añadir: **New repository variable** → introduce nombre y valor → **Add variable**

---

## Paso 5 — Activar GitHub Actions

GitHub Actions se activa automáticamente al detectar `.github/workflows/rockpress_daily.yml`.

El cron está configurado para las **8:00 UTC** (9:00h España en invierno / 10:00h en verano).

Para cambiar la hora, edita esta línea en el workflow:
```yaml
- cron: '0 8 * * *'   # minuto hora * * *  (hora en UTC)
```

Ejemplos:
- `'0 7 * * *'` → 8:00 CET / 9:00 CEST
- `'0 6 * * 1-5'` → solo días laborables a las 7:00 UTC

---

## Paso 6 — Ejecución manual (para probar)

1. Ve a la pestaña **Actions** de tu repositorio
2. Selecciona **"RockPress Tracker – Informe Diario"**
3. Haz clic en **"Run workflow"**
4. Opcional: ajusta los parámetros de entrada:
   - **Días de ventana**: cuántos días hacia atrás buscar (por defecto 3)
   - **Modelo Gemini**: `gemini-3.5-flash` (por defecto), `gemini-1.5-pro`, `gemini-2.0-flash`
   - **Enviar email**: `true` / `false`
5. Pulsa el botón verde **"Run workflow"**

El informe aparecerá en `informes/informe_YYYYMMDD.md` y `seen_articles.json` se actualizará.

---

## Ejecución local (para desarrollo/pruebas)

```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar variables (o añadirlas a tu .zshrc/.bashrc)
export GEMINI_API_KEY="AIzaSy..."
export RESEND_API_KEY="re_..."        # opcional
export EMAIL_TO="jahipe@gmail.com"    # opcional
export EMAIL_FROM="RockPress Tracker <onboarding@resend.dev>"  # opcional
export SEND_EMAIL="true"              # opcional, por defecto true

# Ejecutar
python rockpress_tracker.py
```

Para ejecutar sin enviar email:
```bash
SEND_EMAIL=false python rockpress_tracker.py
```

---

## Todas las variables de entorno

| Variable | Por defecto | Tipo | Descripción |
|---|---|---|---|
| `GEMINI_API_KEY` | — | Secret | **Obligatoria.** Clave de Google AI Studio |
| `RESEND_API_KEY` | — | Secret | Clave de Resend. Sin ella, el email se omite |
| `EMAIL_TO` | — | Variable | Email(s) destinatario, separados por coma |
| `EMAIL_FROM` | `RockPress Tracker <onboarding@resend.dev>` | Variable | Remitente del email |
| `AI_MODEL` | `gemini-3.5-flash` | Variable | Modelo a usar (gpt-4o, claude-3-5-sonnet, gemini...) |
| `DATE_WINDOW_DAYS` | `3` | Variable | Días hacia atrás para buscar noticias |
| `SEND_EMAIL` | `true` | Variable | Activa/desactiva el envío de email |

---

## Estrategia de fetch por niveles

El script intenta obtener artículos de cada medio en este orden, pasando al siguiente si el anterior falla:

| Nivel | Estrategia | Cuándo se usa | Ventaja |
|---|---|---|---|
| 1️⃣ | **WP archive pages** `/YYYY/MM/DD/` | Sitios WordPress confirmados | Solo artículos del día, muy compacto |
| 2️⃣ | **RSS feed** | Si el medio tiene feed en `bbddMedios.md` | `requests` descomprime gzip automáticamente — funciona donde `wget` falla |
| 3️⃣ | **Homepage HTML** | Fallback final | Siempre disponible, Gemini filtra por fecha |

El RSS (nivel 2) resuelve el problema de sitios con HTTP 403/500/SSL: aunque la web principal falle, el feed suele seguir accesible y ya viene con fechas precisas para filtrar.

Para añadir un feed RSS a un medio, completa la tercera columna de `bbddMedios.md`:
```
| Nombre del Medio | https://url.com | https://url.com/feed/ |
```

---

## Sistema de memoria (`seen_articles.json`)

El script mantiene un fichero de memoria en `informes/seen_articles.json` que se commitea al repo tras cada ejecución. Contiene las URLs de todos los artículos ya reportados con su fecha de publicación.

En cada ejecución:
1. Carga las URLs conocidas
2. Las pasa a Gemini para que las excluya del nuevo informe
3. Añade las URLs del nuevo informe a la memoria
4. Purga automáticamente las entradas con más de 30 días de antigüedad

El fichero tiene este formato:
```json
{
  "version": "1.0",
  "last_run": "2026-06-19T10:03:22",
  "total_seen": 47,
  "retention_days": 30,
  "seen": {
    "https://metalcry.com/articulo-ejemplo/": "2026-06-19",
    "https://mariskalrock.com/otra-noticia/": "2026-06-18"
  }
}
```

---

## Añadir o quitar medios

Edita `bbddMedios.md` y añade filas a la tabla:

```
| Nombre del Medio | https://url-del-medio.com | https://url-del-medio.com/feed/ |
```

Si es un sitio WordPress, añade también su URL base en el diccionario `WP_ARCHIVE_SITES`
dentro de `rockpress_tracker.py` para que use archive pages diarias (más eficiente y completo):

```python
WP_ARCHIVE_SITES = {
    "Nombre del Medio": "https://url-del-medio.com",
    # ...
}
```

---

## Estructura de ficheros generados

```
informes/
├── seen_articles.json    ← memoria persistente (auto-actualizada)
├── informe_20260619.md
├── informe_20260620.md
└── ...
```

---

## Coste estimado

| Servicio | Plan | Coste |
|---|---|---|
| Gemini Flash | Gratuito (1.500 llamadas/día) | 0 € |
| Resend | Gratuito (3.000 emails/mes) | 0 € |
| GitHub Actions | Gratuito (2.000 min/mes en repos privados) | 0 € |
| **Total** | | **0 €/mes** |

Cada ejecución del tracker consume ~5 minutos de GitHub Actions y ~$0.001-0.003 si superas el tier gratuito de Gemini.

---

*Última actualización: junio 2026*
