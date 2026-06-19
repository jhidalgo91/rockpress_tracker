#!/usr/bin/env python3
"""
RockPress Tracker – Informe Diario de Rock Nacional
Fetcha medios de rock español, usa Gemini para generar el informe,
mantiene memoria de artículos ya reportados y envía el informe por email (Resend).
"""

import os
import sys
import re
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rockpress")

# API Keys y configuración (desde variables de entorno / GitHub Secrets)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "RockPress Tracker <onboarding@resend.dev>")
EMAIL_TO       = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

DATE_WINDOW         = int(os.environ.get("DATE_WINDOW_DAYS", "3"))
SEEN_RETENTION_DAYS = 30   # Días que se mantienen las URLs en memoria
SEND_EMAIL          = os.environ.get("SEND_EMAIL", "true").lower() == "true"

# Rutas
SCRIPT_DIR  = Path(__file__).parent
BBDD_FILE   = SCRIPT_DIR / "bbddMedios.md"
OUTPUT_DIR  = SCRIPT_DIR / "informes"
SEEN_FILE   = OUTPUT_DIR / "seen_articles.json"

FETCH_TIMEOUT  = 15
FETCH_DELAY    = 0.8
MAX_CHARS_SITE = 20000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

# Sitios WordPress con soporte confirmado de archive pages /YYYY/MM/DD/
WP_ARCHIVE_SITES = {
    "Metalcry":       "https://metalcry.com",
    "Ballesterock":   "https://ballesterockmusic.com",
    "TNT Radio Rock": "https://tntradiorock.com",
    "Noche de Rock":  "https://www.nochederock.com",
    "Rock Sesion":    "https://rocksesion.com",
    "The Sentinel":   "https://www.thesentinel.es/wpsentinel",
}

# ===========================================================================
# MÓDULO 1: CARGA DE MEDIOS
# ===========================================================================

def load_media(path: Path) -> list[dict]:
    """Parsea bbddMedios.md. Devuelve lista de {nombre, url, rss}."""
    media = []
    if not path.exists():
        log.error(f"No se encuentra {path}")
        return media

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        if re.search(r"[-]{3,}", line):
            continue
        if "Medio" in line and "Web" in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]

        if not parts:
            continue

        nombre = parts[0]
        url    = parts[1] if len(parts) > 1 else ""
        rss    = parts[2] if len(parts) > 2 else ""

        if url.startswith("http"):
            media.append({"nombre": nombre, "url": url.rstrip("/"), "rss": rss})

    log.info(f"Medios con URL: {len(media)}")
    return media

# ===========================================================================
# MÓDULO 2: FETCH Y PARSING
# ===========================================================================

def fetch_html(url: str) -> tuple[str | None, str]:
    """Descarga una URL. Devuelve (html, url_final) o (None, '')."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
        log.warning(f"  HTTP {r.status_code} → {url}")
        return None, ""
    except requests.exceptions.RequestException as e:
        log.warning(f"  Error {type(e).__name__}: {url}")
        return None, ""


def html_to_clean_text(html: str, max_chars: int = MAX_CHARS_SITE) -> str:
    """Convierte HTML a texto limpio, sin scripts/nav/footer."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "form", "noscript", "iframe", "svg",
                     "button", "figure"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l for l in text.splitlines() if len(l.strip()) > 3]
    return "\n".join(lines)[:max_chars]


def is_archive_page(final_url: str, target_date: datetime) -> bool:
    """Verifica que la URL final contiene la fecha (evita redirecciones a home)."""
    return target_date.strftime("%Y/%m/%d") in final_url


def collect_content(media: list[dict]) -> dict[str, str]:
    """
    Recopila contenido de cada medio para los últimos DATE_WINDOW días.
    Estrategia 1: WordPress archive pages /YYYY/MM/DD/
    Estrategia 2: Homepage (fallback)
    """
    dates = [datetime.now() - timedelta(days=i) for i in range(DATE_WINDOW)]
    collected: dict[str, str] = {}

    for m in media:
        nombre   = m["nombre"]
        url_base = m["url"]
        textos   = []

        log.info(f"→ {nombre}")

        # Estrategia 1: WordPress archive pages
        if nombre in WP_ARCHIVE_SITES:
            wp_base = WP_ARCHIVE_SITES[nombre]
            for d in dates:
                archive_url = f"{wp_base}/{d.strftime('%Y/%m/%d')}/"
                html, final_url = fetch_html(archive_url)
                time.sleep(FETCH_DELAY)

                if html and is_archive_page(final_url, d):
                    texto = html_to_clean_text(html)
                    if texto:
                        textos.append(f"[ARCHIVE {d.strftime('%d/%m/%Y')}]\n{texto}")
                        log.info(f"  ✅ archive {d.strftime('%d/%m/%Y')}: {len(texto):,} chars")
                else:
                    log.info(f"  ⚠️  archive {d.strftime('%d/%m/%Y')}: no disponible")

        # Estrategia 2: Homepage
        if not textos:
            html, _ = fetch_html(url_base)
            time.sleep(FETCH_DELAY)
            if html:
                texto = html_to_clean_text(html, max_chars=25000)
                if texto:
                    textos.append(f"[HOMEPAGE]\n{texto}")
                    log.info(f"  ✅ homepage: {len(texto):,} chars")
                else:
                    log.info(f"  ⚠️  homepage vacía")
            else:
                log.info(f"  ❌ sin acceso")

        collected[nombre] = "\n\n---\n\n".join(textos) if textos else ""

    activos = sum(1 for v in collected.values() if v)
    log.info(f"Medios con contenido: {activos}/{len(media)}")
    return collected

# ===========================================================================
# MÓDULO 3: MEMORIA (seen_articles.json)
# ===========================================================================

def load_seen_urls() -> dict[str, str]:
    """
    Carga el fichero de memoria seen_articles.json.
    Devuelve dict {url: "YYYY-MM-DD"} con los artículos ya reportados.
    """
    if not SEEN_FILE.exists():
        log.info("Primera ejecución — sin memoria previa")
        return {}

    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen = data.get("seen", {})
        log.info(f"Memoria cargada: {len(seen)} artículos conocidos")
        return seen
    except Exception as e:
        log.warning(f"No se pudo cargar seen_articles.json: {e}")
        return {}


def save_seen_urls(seen: dict[str, str]) -> None:
    """
    Guarda el fichero de memoria, purgando entradas más antiguas
    que SEEN_RETENTION_DAYS días.
    """
    cutoff = (datetime.now() - timedelta(days=SEEN_RETENTION_DAYS)).strftime("%Y-%m-%d")
    seen_clean = {url: date for url, date in seen.items() if date >= cutoff}
    purgados = len(seen) - len(seen_clean)

    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        "version": "1.0",
        "last_run": datetime.now().isoformat(),
        "total_seen": len(seen_clean),
        "retention_days": SEEN_RETENTION_DAYS,
        "seen": seen_clean,
    }
    SEEN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info(
        f"Memoria guardada: {len(seen_clean)} URLs "
        f"({purgados} purgadas por antigüedad)"
    )


def extract_urls_from_report(report: str) -> list[str]:
    """
    Extrae las URLs del informe generado por Gemini.
    Busca líneas con 'Enlace: https://...'
    """
    pattern = r"Enlace:\s*(https?://[^\s\n\)]+)"
    urls = re.findall(pattern, report)
    log.info(f"URLs extraídas del informe: {len(urls)}")
    return urls

# ===========================================================================
# MÓDULO 4: GEMINI – PROMPT Y LLAMADA
# ===========================================================================

def build_prompt(
    collected: dict[str, str],
    today_str: str,
    desde_str: str,
    seen_urls: dict[str, str],
) -> str:
    """Construye el prompt completo para Gemini, incluyendo los artículos a excluir."""

    bloques = []
    for nombre, texto in collected.items():
        if texto:
            bloques.append(
                f"{'='*60}\nMEDIO: {nombre}\n{'='*60}\n{texto[:MAX_CHARS_SITE]}"
            )

    contenido_total = "\n\n".join(bloques)
    todos_los_medios = ", ".join(collected.keys())

    # Lista de URLs ya vistas (máximo 300 para no saturar el contexto)
    seen_list_str = ""
    if seen_urls:
        seen_sample = list(seen_urls.keys())[:300]
        seen_list_str = "\n".join(f"- {url}" for url in seen_sample)
    else:
        seen_list_str = "(ninguna — primera ejecución)"

    return f"""Eres un periodista musical especializado en rock nacional español.

FECHA DE HOY: {today_str}
VENTANA DE NOTICIAS: solo noticias publicadas entre {desde_str} y {today_str}.
MEDIOS CONSULTADOS: {todos_los_medios}

═══════════════════════════════════════════════════════════
ARTÍCULOS YA REPORTADOS EN EJECUCIONES ANTERIORES
(EXCLUYE COMPLETAMENTE estos artículos del nuevo informe)
═══════════════════════════════════════════════════════════
{seen_list_str}

═══════════════════════════════════════════════════════════
TAREA
═══════════════════════════════════════════════════════════
1. Lee el contenido de cada medio.
2. Identifica noticias publicadas en la ventana de fechas indicada.
3. EXCLUYE cualquier artículo cuya URL aparezca en la lista de artículos ya reportados.
4. Filtra solo noticias relacionadas con:
   - Bandas o artistas de rock/metal ESPAÑOLES
   - Festivales o conciertos en ESPAÑA
   - Lanzamientos de artistas ESPAÑOLES
   - Entrevistas a artistas ESPAÑOLES
   - Salas, promotores, sellos o eventos en ESPAÑA
   - Artistas internacionales con conciertos anunciados en España (relevancia media)
5. Para cada noticia extrae: titular exacto, medio, URL, resumen (2-3 frases), categoría, relevancia ⭐1-5.
6. Si no hay noticias nuevas (todas ya fueron reportadas), indícalo explícitamente.

FORMATO DE SALIDA (usa exactamente este formato):

# INFORME DIARIO – ROCK NACIONAL
Fecha: {today_str}

---

## 📰 Titulares Relevantes

---

### 🔴 ALTA RELEVANCIA

**N. TITULAR COMPLETO** — NOMBRE_MEDIO
Resumen: [2-3 frases]
Enlace: [URL]
Categoría: [Lanzamiento / Festival / Concierto / Entrevista / Gira / Bandas españolas / etc.]
Relevancia: ⭐⭐⭐⭐⭐ — [justificación breve]

---

### 🟠 MEDIA-ALTA RELEVANCIA

[mismo formato]

---

### 🟡 MEDIA RELEVANCIA

[mismo formato]

---

## 📊 Resumen General

- **Total de noticias NUEVAS encontradas:** X
- **Artículos descartados por ya reportados:** Y
- **Medios con actividad relevante:** [lista]
- **Tendencias detectadas:** [2-3 frases]

---

## 🗂️ Fuentes consultadas

| Medio | Estado | Noticias nuevas |
|---|---|---|
| [nombre] | ✅ Activo / ⚠️ Sin noticias nuevas / ❌ Sin acceso | Sí / No |

---

*Informe generado automáticamente por RockPress Tracker · {today_str}*

═══════════════════════════════════════════════════════════
CONTENIDO PARA ANALIZAR:
═══════════════════════════════════════════════════════════
{contenido_total}
"""


def call_gemini(prompt: str) -> str:
    """Llama a la API de Gemini y devuelve el texto generado."""
    genai.configure(api_key=GEMINI_API_KEY)

    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        generation_config=genai.types.GenerationConfig(
            temperature=0.1,
            max_output_tokens=8192,
        ),
    )

    log.info(f"Enviando {len(prompt):,} chars a Gemini ({GEMINI_MODEL})…")
    response = model.generate_content(prompt)

    if not response.text:
        raise RuntimeError("Gemini devolvió respuesta vacía")

    log.info(f"Respuesta: {len(response.text):,} chars")
    return response.text

# ===========================================================================
# MÓDULO 5: EMAIL (Resend)
# ===========================================================================

_MONTH_ES = {
    "January": "enero", "February": "febrero", "March": "marzo",
    "April": "abril", "May": "mayo", "June": "junio",
    "July": "julio", "August": "agosto", "September": "septiembre",
    "October": "octubre", "November": "noviembre", "December": "diciembre",
}


def markdown_to_html(md: str) -> str:
    """Convierte el informe markdown a HTML con estilos para email."""
    h = md

    # Convertir markdown a HTML (orden importa)
    h = re.sub(r"^### (.+)$",  r"<h3>\1</h3>",  h, flags=re.MULTILINE)
    h = re.sub(r"^## (.+)$",   r"<h2>\1</h2>",  h, flags=re.MULTILINE)
    h = re.sub(r"^# (.+)$",    r"<h1>\1</h1>",  h, flags=re.MULTILINE)
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"\*(.+?)\*",     r"<em>\1</em>", h)
    h = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', h)
    h = re.sub(r"^---+$", "<hr>", h, flags=re.MULTILINE)

    # Convertir líneas de tabla markdown a HTML básico
    def table_to_html(match):
        rows = [r for r in match.group(0).strip().splitlines() if "|" in r and "---" not in r]
        html_rows = []
        for i, row in enumerate(rows):
            cells = [c.strip() for c in row.strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            html_rows.append(
                "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"
            )
        return "<table>" + "".join(html_rows) + "</table>"

    h = re.sub(r"(\|.+\n)+", table_to_html, h)

    # Saltos de línea
    h = h.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 16px;
    color: #2c2c2c;
    background: #f9f9f9;
  }}
  .card {{
    background: #ffffff;
    border-radius: 8px;
    padding: 32px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  }}
  h1 {{
    color: #c0392b;
    border-bottom: 3px solid #c0392b;
    padding-bottom: 10px;
    font-size: 24px;
  }}
  h2 {{
    color: #c0392b;
    font-size: 18px;
    margin-top: 28px;
    margin-bottom: 8px;
  }}
  h3 {{
    color: #555;
    font-size: 15px;
    margin-top: 20px;
    border-left: 3px solid #e74c3c;
    padding-left: 10px;
  }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 20px 0; }}
  a {{ color: #c0392b; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  strong {{ color: #1a1a1a; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 13px;
  }}
  th, td {{
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
  }}
  th {{ background: #f4f4f4; font-weight: 600; }}
  .footer {{
    text-align: center;
    color: #aaa;
    font-size: 11px;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid #eee;
  }}
</style>
</head>
<body>
<div class="card">
{h}
</div>
<div class="footer">
  RockPress Tracker · Generado automáticamente · <a href="https://github.com">Ver en GitHub</a>
</div>
</body>
</html>"""


def send_email(subject: str, html_content: str, text_content: str) -> bool:
    """Envía el informe por email usando la API de Resend."""
    if not RESEND_API_KEY:
        log.info("RESEND_API_KEY no configurada → email omitido")
        return False

    if not EMAIL_TO:
        log.warning("EMAIL_TO no configurada → email omitido")
        return False

    try:
        import resend  # noqa: PLC0415
        resend.api_key = RESEND_API_KEY

        params: resend.Emails.SendParams = {
            "from_": EMAIL_FROM,
            "to":    EMAIL_TO,
            "subject": subject,
            "html":    html_content,
            "text":    text_content,
        }

        result = resend.Emails.send(params)
        email_id = result.get("id", "N/A") if isinstance(result, dict) else getattr(result, "id", "N/A")
        log.info(f"✅ Email enviado a {EMAIL_TO} — ID: {email_id}")
        return True

    except ImportError:
        log.error("Paquete 'resend' no instalado. Ejecuta: pip install resend")
        return False
    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return False

# ===========================================================================
# MÓDULO 6: GUARDAR INFORME
# ===========================================================================

def date_to_spanish(dt: datetime) -> str:
    """Formatea una fecha como '19 de junio de 2026'."""
    raw = dt.strftime("%-d de %B de %Y")
    for en, es in _MONTH_ES.items():
        raw = raw.replace(en, es)
    return raw


def save_report(content: str, date: datetime) -> Path:
    """Guarda el informe en informes/informe_YYYYMMDD.md"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = OUTPUT_DIR / f"informe_{date.strftime('%Y%m%d')}.md"
    filename.write_text(content, encoding="utf-8")
    log.info(f"Informe guardado: {filename.name}")
    return filename

# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY no configurada.")
        log.error("Obtén tu clave en: https://aistudio.google.com/app/apikey")
        sys.exit(1)

    today  = datetime.now()
    desde  = today - timedelta(days=DATE_WINDOW)
    today_str = date_to_spanish(today)
    desde_str = desde.strftime("%d/%m/%Y")

    log.info("=" * 65)
    log.info(f"  RockPress Tracker · {today_str}")
    log.info(f"  Ventana: {desde_str} → {today.strftime('%d/%m/%Y')}")
    log.info("=" * 65)

    # 1. Cargar medios
    media = load_media(BBDD_FILE)
    if not media:
        log.error("Sin medios en bbddMedios.md. Abortando.")
        sys.exit(1)

    # 2. Cargar memoria de artículos ya reportados
    seen_urls = load_seen_urls()

    # 3. Recopilar contenido de los medios
    collected = collect_content(media)
    if not any(collected.values()):
        log.error("Sin contenido accesible. Abortando.")
        sys.exit(1)

    # 4. Generar informe con Gemini
    prompt = build_prompt(collected, today_str, desde_str, seen_urls)
    try:
        report = call_gemini(prompt)
    except Exception as e:
        log.error(f"Error con Gemini: {e}")
        sys.exit(1)

    # 5. Guardar informe en disco
    report_path = save_report(report, today)

    # 6. Actualizar memoria con los nuevos artículos reportados
    new_urls = extract_urls_from_report(report)
    today_date = today.strftime("%Y-%m-%d")
    for url in new_urls:
        seen_urls[url] = today_date
    save_seen_urls(seen_urls)

    # 7. Enviar por email (si está configurado)
    if SEND_EMAIL:
        subject = f"🎸 RockPress – Rock Nacional {today.strftime('%d/%m/%Y')}"
        html    = markdown_to_html(report)
        send_email(subject, html, report)
    else:
        log.info("Envío de email desactivado (SEND_EMAIL=false)")

    log.info("=" * 65)
    log.info(f"✅ Proceso completado → {report_path.name}")
    log.info("=" * 65)

    # Imprimir en stdout (útil para logs de GitHub Actions)
    print("\n" + report)


if __name__ == "__main__":
    main()
