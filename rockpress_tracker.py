#!/usr/bin/env python3
"""
RockPress Tracker – Informe Diario de Rock Nacional
Arquitectura optimizada:
  - Python extrae artículos estructurados (título + URL + fecha) con BeautifulSoup
  - Gemini recibe una lista compacta (~20K chars) en lugar de texto HTML bruto
  - Memoria persistente en seen_articles.json para evitar repetir noticias
  - Envío de informe por email via Resend
"""

import os
import sys
import re
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import openai
import anthropic

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rockpress")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL       = os.environ.get("AI_MODEL", "gemini-3.5-flash")
API_DELAY_SECONDS = int(os.environ.get("API_DELAY_SECONDS", "120"))
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "RockPress Tracker <onboarding@resend.dev>")
EMAIL_TO       = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

DATE_WINDOW         = int(os.environ.get("DATE_WINDOW_DAYS", "3"))
SEEN_RETENTION_DAYS = 30
SEND_EMAIL_FLAG     = os.environ.get("SEND_EMAIL", "true").lower() == "true"

SCRIPT_DIR = Path(__file__).parent
BBDD_FILE  = SCRIPT_DIR / "bbddMedios.md"
OUTPUT_DIR = SCRIPT_DIR / "informes"
SEEN_FILE  = OUTPUT_DIR / "seen_articles.json"

FETCH_TIMEOUT = 15
FETCH_DELAY   = 0.6
MAX_EXCERPT   = 180   # chars del extracto de cada artículo

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

# Sitios WordPress con archive pages /YYYY/MM/DD/ confirmadas
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
        parts = [p.strip() for p in line.split("|") if p.strip()]
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
# MÓDULO 2: FETCH
# ===========================================================================

def fetch_html(url: str) -> tuple[str | None, str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
        log.warning(f"  HTTP {r.status_code} → {url}")
        return None, ""
    except requests.exceptions.RequestException as e:
        log.warning(f"  {type(e).__name__}: {url}")
        return None, ""


def is_archive_page(final_url: str, target_date: datetime) -> bool:
    return target_date.strftime("%Y/%m/%d") in final_url

# ===========================================================================
# MÓDULO 3A: EXTRACCIÓN DESDE RSS
# ===========================================================================

def fetch_rss(rss_url: str, window_start: datetime, window_end: datetime) -> list[dict]:
    """
    Descarga y parsea un feed RSS/Atom.
    requests descomprime gzip automáticamente → no hay problema con feeds comprimidos.
    Devuelve lista de {title, url, date, excerpt} filtrada por ventana de fechas.
    """
    import feedparser  # noqa: PLC0415

    try:
        # Descargamos el feed con nuestros headers (incluye Accept-Encoding: gzip)
        # requests descomprime automáticamente → r.content es el XML limpio
        r = requests.get(rss_url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            log.warning(f"  RSS HTTP {r.status_code} → {rss_url}")
            return []

        # feedparser acepta bytes directamente
        feed = feedparser.parse(r.content)

        if feed.bozo and not feed.entries:
            log.warning(f"  RSS malformado: {rss_url}")
            return []

        articles: list[dict] = []

        for entry in feed.entries:
            # Título
            title = _clean(entry.get("title", ""))
            if len(title) < 5:
                continue

            # URL
            url = entry.get("link", "")
            if not url or not url.startswith("http"):
                continue

            # Fecha — feedparser normaliza a struct_time
            date_str = ""
            for date_field in ("published_parsed", "updated_parsed", "created_parsed"):
                parsed = getattr(entry, date_field, None)
                if parsed:
                    try:
                        dt = datetime(*parsed[:6])
                        date_str = dt.strftime("%Y-%m-%d")
                        break
                    except (ValueError, TypeError):
                        continue

            # Filtrar por ventana de fechas (si tenemos fecha)
            if date_str and not date_in_window(date_str, window_start, window_end):
                continue

            # Extracto: content > summary
            excerpt = ""
            if hasattr(entry, "content") and entry.content:
                raw = entry.content[0].get("value", "")
            else:
                raw = entry.get("summary", "")
            if raw:
                excerpt = _clean(BeautifulSoup(raw, "html.parser").get_text())[:MAX_EXCERPT]

            articles.append({
                "title":   title[:220],
                "url":     url,
                "date":    date_str or window_end.strftime("%Y-%m-%d"),
                "excerpt": excerpt,
            })

        return articles

    except Exception as e:
        log.warning(f"  Error RSS ({type(e).__name__}): {rss_url}")
        return []


# ===========================================================================
# MÓDULO 3B: EXTRACCIÓN ESTRUCTURADA DESDE HTML
# ===========================================================================

def _clean(text: str) -> str:
    """Limpia y normaliza texto."""
    return re.sub(r"\s+", " ", text).strip()


def extract_articles(html: str, fallback_date: str) -> list[dict]:
    """
    Extrae artículos de una página HTML.
    Devuelve lista de {title, url, date, excerpt}.
    Python hace el trabajo pesado aquí — Gemini solo clasifica.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict] = []
    seen_urls: set[str] = set()

    # ---- Patrón 1: tags <article> (WordPress estándar) ----
    for art in soup.find_all("article"):
        # Título: buscar en h1/h2/h3 dentro del article
        title_tag = art.find(["h1", "h2", "h3"])
        if not title_tag:
            continue
        title = _clean(title_tag.get_text())
        if len(title) < 5:
            continue

        # URL: preferir el enlace del título
        a = title_tag.find("a", href=True) or art.find("a", href=True)
        if not a:
            continue
        url = a.get("href", "")
        if not url.startswith("http") or url in seen_urls:
            continue
        # Excluir URLs de categorías/tags/página (sin slug de artículo real)
        path = urlparse(url).path.strip("/")
        if not path or path.count("/") < 1:
            continue
        seen_urls.add(url)

        # Fecha: tag <time> con datetime o texto
        date = fallback_date
        time_tag = art.find("time")
        if time_tag:
            dt = time_tag.get("datetime", "")
            date = dt[:10] if len(dt) >= 10 else fallback_date

        # Extracto: primer <p> con texto real
        excerpt = ""
        for p in art.find_all("p"):
            t = _clean(p.get_text())
            if len(t) > 30:
                excerpt = t[:MAX_EXCERPT]
                break

        articles.append({
            "title":   title[:220],
            "url":     url,
            "date":    date,
            "excerpt": excerpt,
        })

    # ---- Patrón 2: h2/h3 con enlaces (blogs, webs sin <article>) ----
    if len(articles) < 3:
        for h in soup.find_all(["h2", "h3"]):
            a = h.find("a", href=True)
            if not a:
                continue
            url = a.get("href", "")
            if not url.startswith("http") or url in seen_urls:
                continue
            path = urlparse(url).path.strip("/")
            if not path or len(path) < 5:
                continue
            # Evitar enlaces de menú (texto muy corto o genérico)
            title = _clean(h.get_text())
            if len(title) < 8 or title.lower() in (
                "inicio", "home", "noticias", "blog", "contacto", "artículos"
            ):
                continue
            seen_urls.add(url)
            articles.append({
                "title":   title[:220],
                "url":     url,
                "date":    fallback_date,
                "excerpt": "",
            })

    return articles


def date_in_window(date_str: str, window_start: datetime, window_end: datetime) -> bool:
    """Comprueba si una fecha (YYYY-MM-DD) está dentro de la ventana."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return window_start <= d <= window_end
    except ValueError:
        return True  # Si no podemos parsear, lo incluimos (Gemini filtrará)

# ===========================================================================
# MÓDULO 4: RECOPILACIÓN DE ARTÍCULOS
# ===========================================================================

def collect_articles(media: list[dict]) -> dict[str, list[dict]]:
    """
    Para cada medio, intenta obtener artículos usando 3 estrategias en cascada:
      1. WordPress archive pages /YYYY/MM/DD/ (para sitios WP confirmados)
      2. RSS feed               (requests descomprime gzip → funciona en todos los feeds)
      3. Homepage HTML          (fallback final)
    Devuelve dict {nombre_medio: [artículos en ventana de fechas]}.
    """
    today        = datetime.now()
    window_end   = today
    window_start = today - timedelta(days=DATE_WINDOW)
    dates        = [today - timedelta(days=i) for i in range(DATE_WINDOW)]
    collected: dict[str, list[dict]] = {}

    for m in media:
        nombre   = m["nombre"]
        url_base = m["url"]
        rss_url  = m.get("rss", "")
        all_articles: list[dict] = []
        strategy_used = ""

        log.info(f"→ {nombre}")

        # ── ESTRATEGIA 1: WordPress archive pages (/YYYY/MM/DD/) ──────────────
        if nombre in WP_ARCHIVE_SITES:
            wp_base = WP_ARCHIVE_SITES[nombre]
            for d in dates:
                archive_url = f"{wp_base}/{d.strftime('%Y/%m/%d')}/"
                html, final_url = fetch_html(archive_url)
                time.sleep(FETCH_DELAY)
                if html and is_archive_page(final_url, d):
                    arts = extract_articles(html, d.strftime("%Y-%m-%d"))
                    if arts:
                        all_articles.extend(arts)
                        if not strategy_used:
                            strategy_used = "WP archive"
                        log.info(f"  ✅ archive {d.strftime('%d/%m')}: {len(arts)} artículos")
                    else:
                        log.info(f"  ⚠️  archive {d.strftime('%d/%m')}: página vacía")
                else:
                    log.info(f"  ⚠️  archive {d.strftime('%d/%m')}: no disponible (404/redirect)")

        # ── ESTRATEGIA 2: RSS feed ─────────────────────────────────────────────
        # requests descomprime gzip automáticamente — aquí sí funciona lo que
        # fallaba con web_fetch en el rastreo manual
        if not all_articles and rss_url.startswith("http"):
            log.info(f"  → Probando RSS: {rss_url}")
            arts = fetch_rss(rss_url, window_start, window_end)
            if arts:
                all_articles.extend(arts)
                strategy_used = "RSS"
                log.info(f"  ✅ RSS: {len(arts)} artículos en ventana")
            else:
                log.info(f"  ⚠️  RSS: sin artículos en ventana o feed inaccesible")
            time.sleep(FETCH_DELAY)

        # ── ESTRATEGIA 3: Homepage HTML ────────────────────────────────────────
        if not all_articles:
            html, _ = fetch_html(url_base)
            time.sleep(FETCH_DELAY)
            if html:
                arts = extract_articles(html, today.strftime("%Y-%m-%d"))
                if arts:
                    all_articles.extend(arts)
                    strategy_used = "homepage"
                    log.info(f"  ✅ homepage: {len(arts)} artículos extraídos")
                else:
                    log.info(f"  ⚠️  homepage: sin artículos detectados")
            else:
                log.info(f"  ❌ sin acceso por ninguna vía")

        # ── Deduplicar por URL ─────────────────────────────────────────────────
        seen_u: set[str] = set()
        unique: list[dict] = []
        for a in all_articles:
            if a["url"] not in seen_u:
                seen_u.add(a["url"])
                unique.append(a)

        # ── Filtrar por ventana de fechas ──────────────────────────────────────
        # Para artículos con fecha parseada, aplicar filtro estricto.
        # Para homepage (fecha=hoy por defecto), incluir todos y dejar a Gemini filtrar.
        if strategy_used in ("WP archive", "RSS"):
            in_window = [a for a in unique if date_in_window(a["date"], window_start, window_end)]
        else:
            in_window = unique  # homepage: Gemini filtra por fecha

        collected[nombre] = in_window
        if in_window:
            log.info(f"  → {len(in_window)} artículo(s) vía {strategy_used}")

    total_arts = sum(len(v) for v in collected.values())
    medios_con = sum(1 for v in collected.values() if v)
    log.info(f"Total artículos recopilados: {total_arts} de {medios_con}/{len(media)} medios")
    return collected

# ===========================================================================
# MÓDULO 5: MEMORIA
# ===========================================================================

def load_seen_urls() -> dict[str, str]:
    if not SEEN_FILE.exists():
        log.info("Primera ejecución — sin memoria previa")
        return {}
    try:
        data  = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen  = data.get("seen", {})
        log.info(f"Memoria cargada: {len(seen)} URLs conocidas")
        return seen
    except Exception as e:
        log.warning(f"Error cargando seen_articles.json: {e}")
        return {}


def save_seen_urls(seen: dict[str, str]) -> None:
    cutoff     = (datetime.now() - timedelta(days=SEEN_RETENTION_DAYS)).strftime("%Y-%m-%d")
    seen_clean = {url: date for url, date in seen.items() if date >= cutoff}
    purgados   = len(seen) - len(seen_clean)

    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        "version":        "1.0",
        "last_run":       datetime.now().isoformat(),
        "total_seen":     len(seen_clean),
        "retention_days": SEEN_RETENTION_DAYS,
        "seen":           seen_clean,
    }
    SEEN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Memoria guardada: {len(seen_clean)} URLs ({purgados} purgadas)")


def extract_urls_from_report(report: str) -> list[str]:
    return re.findall(r"Enlace:\s*(https?://[^\s\n\)]+)", report)

# ===========================================================================
# MÓDULO 6: GEMINI
# ===========================================================================

def build_article_list(
    collected: dict[str, list[dict]],
    seen_urls: dict[str, str],
) -> tuple[str, int, int]:
    """
    Construye la lista compacta de artículos para el prompt.
    Devuelve (texto, total_nuevos, total_ya_vistos).
    """
    lines: list[str] = []
    total_nuevos = 0
    total_vistos = 0

    for medio, articles in collected.items():
        if not articles:
            continue
        nuevos = [a for a in articles if a["url"] not in seen_urls]
        vistos = len(articles) - len(nuevos)
        total_vistos  += vistos
        total_nuevos  += len(nuevos)

        if not nuevos:
            continue

        lines.append(f"\n{'─'*50}")
        lines.append(f"MEDIO: {medio}")
        lines.append(f"{'─'*50}")
        for a in nuevos:
            lines.append(f"• [{a['date']}] {a['title']}")
            lines.append(f"  URL: {a['url']}")
            if a["excerpt"]:
                lines.append(f"  Resumen: {a['excerpt']}")

    return "\n".join(lines), total_nuevos, total_vistos


_MONTH_ES = {
    "January":"enero","February":"febrero","March":"marzo","April":"abril",
    "May":"mayo","June":"junio","July":"julio","August":"agosto",
    "September":"septiembre","October":"octubre","November":"noviembre","December":"diciembre",
}

def date_to_spanish(dt: datetime) -> str:
    raw = dt.strftime("%-d de %B de %Y")
    for en, es in _MONTH_ES.items():
        raw = raw.replace(en, es)
    return raw


BATCH_SIZE = 30  # artículos por llamada de clasificación a Gemini


def _call_ai_raw(prompt: str, max_output_tokens: int, debug_label: str = "ai") -> tuple[str, str]:
    """
    Llamada base a la IA. Devuelve (response_text, finish_reason).
    Soporta Gemini, OpenAI y Anthropic.
    Aplica delay de 2 minutos antes de realizar la petición.
    """
    if API_DELAY_SECONDS > 0:
        log.info(f"  [{debug_label}] Esperando {API_DELAY_SECONDS}s para evitar saturación de API...")
        time.sleep(API_DELAY_SECONDS)

    is_batch = debug_label.startswith("batch_")
    
    system_instr = (
        "Eres un sistema de clasificación de noticias de rock. "
        "Responde ÚNICAMENTE con JSON válido. "
        "NUNCA incluyas razonamiento, explicaciones ni texto fuera del JSON."
    ) if is_batch else "Eres un periodista musical especializado en rock nacional espanol."

    log.info(f"  [{debug_label}] Enviando {len(prompt):,} chars a {AI_MODEL} (max_tokens={max_output_tokens})…")
    
    response_text = ""
    finish_reason = "UNKNOWN"
    input_tokens = 0
    output_tokens = 0

    try:
        if AI_MODEL.startswith("gpt"):
            client = openai.Client(api_key=OPENAI_API_KEY)
            messages = []
            if system_instr:
                messages.append({"role": "system", "content": system_instr})
            messages.append({"role": "user", "content": prompt})
            
            kwargs = {"model": AI_MODEL, "messages": messages, "temperature": 0.1, "max_tokens": max_output_tokens}
            if is_batch:
                kwargs["response_format"] = {"type": "json_object"}
                
            res = client.chat.completions.create(**kwargs)
            response_text = res.choices[0].message.content or ""
            finish_reason = res.choices[0].finish_reason
            if res.usage:
                input_tokens = res.usage.prompt_tokens
                output_tokens = res.usage.completion_tokens

        elif AI_MODEL.startswith("claude"):
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            kwargs = {
                "model": AI_MODEL,
                "max_tokens": max_output_tokens,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": prompt}]
            }
            if system_instr:
                kwargs["system"] = system_instr
                
            res = client.messages.create(**kwargs)
            response_text = res.content[0].text if res.content else ""
            finish_reason = res.stop_reason or "UNKNOWN"
            if hasattr(res, "usage"):
                input_tokens = res.usage.input_tokens
                output_tokens = res.usage.output_tokens

        else:
            # Gemini default
            genai.configure(api_key=GEMINI_API_KEY)
            gen_config_kwargs = {"temperature": 0.1, "max_output_tokens": max_output_tokens}
            if is_batch:
                gen_config_kwargs["response_mime_type"] = "application/json"
            
            # Quitar si falla en SDK viejo
            try:
                gen_config = genai.types.GenerationConfig(**gen_config_kwargs)
            except TypeError:
                gen_config_kwargs.pop("response_mime_type", None)
                gen_config = genai.types.GenerationConfig(**gen_config_kwargs)

            model = genai.GenerativeModel(
                model_name=AI_MODEL,
                generation_config=gen_config,
                system_instruction=system_instr if is_batch else None,
            )
            res = model.generate_content(prompt)
            response_text = res.text if res.text else ""
            
            candidate = res.candidates[0] if res.candidates else None
            finish_reason = str(candidate.finish_reason) if candidate else "UNKNOWN"
            usage = res.usage_metadata
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0

    except Exception as e:
        log.error(f"  [{debug_label}] Error en llamada API: {e}")
        finish_reason = "ERROR"

    log.info(f"  [{debug_label}] finish_reason={finish_reason}  tokens in={input_tokens:,} out={output_tokens:,}")

    # ── Debug log ────────────────────────────────────────────────────────────
    try:
        ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir  = OUTPUT_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^\w\-]", "_", debug_label)
        debug_path = logs_dir / f"debug_{ts_label}_{safe_label}.txt"
        debug_path.write_text(
            f"=== ROCKPRESS DEBUG — {ts_label} — {debug_label} ===\n"
            f"Modelo: {AI_MODEL}\n"
            f"max_output_tokens: {max_output_tokens}\n"
            f"finish_reason: {finish_reason}\n"
            f"Tokens → input: {input_tokens:,}  output: {output_tokens:,}\n"
            f"\n{'='*60}\nPROMPT ({len(prompt):,} chars):\n{'='*60}\n"
            f"{prompt}\n"
            f"\n{'='*60}\nRESPUESTA ({len(response_text):,} chars):\n{'='*60}\n"
            f"{response_text}\n",
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"  [{debug_label}] Error guardando debug log: {e}")

    return response_text, str(finish_reason)


def _articles_to_text(articles_with_medio: list[tuple[str, dict]]) -> str:
    """Formatea lista de (medio, article) como texto legible para el prompt."""
    by_medio: dict[str, list[dict]] = {}
    for medio, art in articles_with_medio:
        by_medio.setdefault(medio, []).append(art)

    lines: list[str] = []
    for medio, arts in by_medio.items():
        lines.append(f"\n{'─'*50}")
        lines.append(f"MEDIO: {medio}")
        lines.append(f"{'─'*50}")
        for a in arts:
            lines.append(f"• [{a['date']}] {a['title']}")
            lines.append(f"  URL: {a['url']}")
            if a.get("excerpt"):
                lines.append(f"  Resumen: {a['excerpt']}")
    return "\n".join(lines)


def classify_batch(
    articles_with_medio: list[tuple[str, dict]],
    batch_num: int,
    total_batches: int,
    today_str: str,
    desde_str: str,
) -> list[dict]:
    """
    Clasifica un lote de artículos contra los criterios de rock nacional español.
    Gemini devuelve JSON puro — respuesta pequeña y fiable (sin riesgo de truncado).
    """
    articles_text = _articles_to_text(articles_with_medio)

    prompt = f"""Eres un filtro de noticias de rock nacional español.
Analiza el lote {batch_num}/{total_batches} ({len(articles_with_medio)} artículos).

FECHA HOY: {today_str}
VENTANA: {desde_str} — {today_str}

CRITERIOS DE INCLUSION (al menos uno):
- Bandas/artistas de rock o metal ESPANOLES
- Festivales o conciertos EN ESPANA
- Lanzamientos (disco/EP/single) de artistas ESPANOLES
- Noticias de salas, promotoras o sellos ESPANOLES
- Artistas internacionales con fecha confirmada en Espana

CRITERIOS DE EXCLUSION:
- Noticias internacionales sin conexion con Espana
- Articulos fuera de la ventana de fechas

TAREA: Devuelve SOLO los articulos relevantes como JSON puro.
Sin markdown, sin explicaciones. Si ningun articulo es relevante: []

ESCALA rel (1-5):
5=banda espanola top / disco nuevo / festival 1 linea
4=artista/evento espanol destacado / entrevista relevante
3=festival regional / banda emergente / concierto en Espana
2=internacional con fecha en Espana
1=mencion menor

FORMATO:
[{{"title":"...","url":"...","date":"YYYY-MM-DD","medio":"...","excerpt":"...","rel":5,"categoria":"Lanzamiento|Festival en Espana|Concierto|Entrevista|Gira|Bandas espanolas"}}]

ARTICULOS:
{articles_text}"""

    log.info(f"Lote {batch_num}/{total_batches}: clasificando {len(articles_with_medio)} artículos…")
    try:
        text, finish_reason = _call_ai_raw(prompt, max_output_tokens=8192, debug_label=f"batch_{batch_num:02d}")

        if not text.strip():
            log.warning(f"  Lote {batch_num}: respuesta vacía (finish_reason={finish_reason})")
            return []

        # Limpiar posible envoltorio ```json ... ``` que Gemini a veces añade
        json_text = text.strip()
        if "```" in json_text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", json_text)
            if m:
                json_text = m.group(1).strip()
        if not json_text.startswith("["):
            m = re.search(r"\[[\s\S]*\]", json_text)
            if m:
                json_text = m.group(0)

        relevant = json.loads(json_text)
        if not isinstance(relevant, list):
            log.warning(f"  Lote {batch_num}: respuesta no es lista JSON")
            return []

        log.info(f"  Lote {batch_num}: {len(relevant)} relevantes de {len(articles_with_medio)}")
        return relevant

    except json.JSONDecodeError as e:
        log.warning(f"  Lote {batch_num}: JSON inválido ({e})")
        return []
    except Exception as e:
        log.warning(f"  Lote {batch_num}: error ({type(e).__name__}: {e})")
        return []


def generate_final_report(
    relevant: list[dict],
    total_vistos: int,
    medios_con_actividad: list[str],
    medios_sin_articulos: list[str],
    today_str: str,
    desde_str: str,
) -> str:
    """
    Genera el informe final Markdown solo con los artículos ya clasificados como relevantes.
    Al ser pocos artículos (~10-20), el prompt es pequeño y la respuesta no se trunca.
    """
    if not relevant:
        return (
            f"# INFORME DIARIO – ROCK NACIONAL\nFecha: {today_str}\n\n"
            f"No se han encontrado noticias de rock nacional español "
            f"en los artículos de los últimos {DATE_WINDOW} días.\n"
        )

    # Ordenar por relevancia desc
    relevant_sorted = sorted(relevant, key=lambda x: x.get("rel", 0), reverse=True)
    medios_act = ", ".join(medios_con_actividad) if medios_con_actividad else "ninguno"
    medios_sin = ", ".join(medios_sin_articulos) if medios_sin_articulos else "ninguno"

    articles_text = "\n\n".join(
        f"{i+1}. [{a.get('date','?')}] {a.get('title','?')}\n"
        f"   Medio: {a.get('medio','?')} | Relevancia: {a.get('rel','?')}/5 | Cat: {a.get('categoria','?')}\n"
        f"   URL: {a.get('url','?')}\n"
        f"   Resumen: {a.get('excerpt') or '(sin extracto)'}"
        for i, a in enumerate(relevant_sorted)
    )

    prompt = f"""Eres un periodista musical especializado en rock nacional espanol.

FECHA: {today_str}
VENTANA: {desde_str} — {today_str}

Tienes {len(relevant)} articulos ya clasificados como relevantes.
Genera el INFORME DIARIO completo en Markdown con este formato exacto:

# INFORME DIARIO - ROCK NACIONAL
Fecha: {today_str}

---

## Titulares Relevantes

---

### ALTA RELEVANCIA (rel 4-5)

**N. TITULAR** - MEDIO
Resumen: [2-3 frases en espanol]
Enlace: [URL completa]
Categoria: [categoria]
Relevancia: [estrellas segun rel] - [justificacion breve]

---

### MEDIA-ALTA RELEVANCIA (rel 3)

[mismo formato]

---

### MEDIA RELEVANCIA (rel 1-2)

[mismo formato]

---

## Resumen General

- Total noticias rock nacional: {len(relevant)}
- Articulos excluidos (ya reportados): {total_vistos}
- Medios con actividad nueva: {medios_act}
- Tendencias detectadas: [2-3 frases]

---

## Fuentes consultadas

| Medio | Estado | Noticias nuevas |
|---|---|---|
[una fila por medio — indica si tiene noticias relevantes o no]

---

*Informe generado automaticamente por RockPress Tracker · {today_str}*

INSTRUCCIONES ADICIONALES:
- Incluye TODOS los {len(relevant)} articulos de la lista abajo.
- Agrupa por nivel de relevancia segun el campo rel.
- Escribe resumenes informativos en espanol.
- En Fuentes, incluye los medios con actividad: {medios_act}
  Y estos sin noticias relevantes: {medios_sin}
- No inventes informacion: usa solo lo que aparece en los articulos.

ARTICULOS CLASIFICADOS:
{articles_text}"""

    log.info(f"Generando informe final con {len(relevant)} artículos…")
    text, finish_reason = _call_ai_raw(prompt, max_output_tokens=16384, debug_label="informe_final")

    if not text:
        raise RuntimeError(f"Gemini no devolvió informe (finish_reason={finish_reason})")
    if finish_reason not in ("FinishReason.STOP", "STOP", "1"):
        log.warning(f"⚠️  Informe puede estar incompleto: finish_reason={finish_reason}")

    return text


def classify_and_report(
    collected: dict[str, list[dict]],
    seen_urls: dict[str, str],
    total_vistos: int,
    medios_sin: list[str],
    medios_con_actividad: list[str],
    today_str: str,
    desde_str: str,
) -> str:
    """
    Orquesta la clasificación en lotes y la generación del informe:
    1. Divide artículos nuevos en lotes de BATCH_SIZE
    2. classify_batch() por lote → JSON con artículos relevantes
    3. generate_final_report() con todos los relevantes acumulados
    """
    # Flatten artículos nuevos con su medio
    all_new: list[tuple[str, dict]] = [
        (medio, art)
        for medio, arts in collected.items()
        for art in arts
        if art["url"] not in seen_urls
    ]

    if not all_new:
        return (
            f"# INFORME DIARIO – ROCK NACIONAL\nFecha: {today_str}\n\n"
            "No se han encontrado artículos nuevos.\n"
        )

    batches       = [all_new[i:i+BATCH_SIZE] for i in range(0, len(all_new), BATCH_SIZE)]
    total_batches = len(batches)
    log.info(f"Clasificando {len(all_new)} artículos en {total_batches} lote(s) de máx. {BATCH_SIZE}")

    all_relevant: list[dict] = []
    for i, batch in enumerate(batches, start=1):
        relevant = classify_batch(batch, i, total_batches, today_str, desde_str)
        all_relevant.extend(relevant)
        if i < total_batches:
            time.sleep(1.5)  # pausa entre llamadas para no saturar la API

    log.info(f"Total relevantes tras clasificación: {len(all_relevant)} de {len(all_new)}")

    return generate_final_report(
        all_relevant, total_vistos, medios_con_actividad, medios_sin, today_str, desde_str
    )

# ===========================================================================
# MÓDULO 7: EMAIL (Resend)
# ===========================================================================

def markdown_to_html(md: str) -> str:
    h = md
    h = re.sub(r"^### (.+)$",    r"<h3>\1</h3>",  h, flags=re.MULTILINE)
    h = re.sub(r"^## (.+)$",     r"<h2>\1</h2>",  h, flags=re.MULTILINE)
    h = re.sub(r"^# (.+)$",      r"<h1>\1</h1>",  h, flags=re.MULTILINE)
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",  h)
    h = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', h)
    h = re.sub(r"^---+$", "<hr>", h, flags=re.MULTILINE)

    def table_to_html(match):
        rows = [r for r in match.group(0).strip().splitlines()
                if "|" in r and "---" not in r]
        result = ["<table>"]
        for i, row in enumerate(rows):
            cells = [c.strip() for c in row.strip("|").split("|")]
            tag   = "th" if i == 0 else "td"
            result.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        result.append("</table>")
        return "\n".join(result)

    h = re.sub(r"(\|.+\n)+", table_to_html, h)
    h = h.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
       max-width:720px;margin:0 auto;padding:24px 16px;color:#2c2c2c;background:#f9f9f9}}
  .card{{background:#fff;border-radius:8px;padding:32px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  h1{{color:#c0392b;border-bottom:3px solid #c0392b;padding-bottom:10px;font-size:24px}}
  h2{{color:#c0392b;font-size:18px;margin-top:28px}}
  h3{{color:#555;font-size:15px;margin-top:20px;border-left:3px solid #e74c3c;padding-left:10px}}
  hr{{border:none;border-top:1px solid #eee;margin:20px 0}}
  a{{color:#c0392b;text-decoration:none}}
  strong{{color:#1a1a1a}}
  table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}
  th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
  th{{background:#f4f4f4;font-weight:600}}
  .footer{{text-align:center;color:#aaa;font-size:11px;margin-top:32px;
           padding-top:16px;border-top:1px solid #eee}}
</style>
</head>
<body>
<div class="card">{h}</div>
<div class="footer">RockPress Tracker · Generado automáticamente</div>
</body>
</html>"""


def send_email(subject: str, html_content: str, text_content: str) -> bool:
    if not RESEND_API_KEY:
        log.info("RESEND_API_KEY no configurada → email omitido")
        return False
    if not EMAIL_TO:
        log.warning("EMAIL_TO no configurada → email omitido")
        return False
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        # Usamos dict plano con "from" como string key (válido en Python aunque sea keyword).
        # Evita el problema de compatibilidad entre versiones del SDK
        # donde unos usan "from_" y otros mapean directamente "from".
        params = {
            "from":    EMAIL_FROM,
            "to":      EMAIL_TO,
            "subject": subject,
            "html":    html_content,
            "text":    text_content,
        }
        result   = resend.Emails.send(params)
        email_id = result.get("id", "N/A") if isinstance(result, dict) else getattr(result, "id", "N/A")
        log.info(f"✅ Email enviado → {EMAIL_TO} (ID: {email_id})")
        return True
    except ImportError:
        log.error("Paquete 'resend' no instalado. Ejecuta: pip install resend")
        return False
    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return False

# ===========================================================================
# MÓDULO 8: GUARDAR INFORME
# ===========================================================================

def save_report(content: str, date: datetime) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = OUTPUT_DIR / f"informe_{date.strftime('%Y%m%d')}.md"
    filename.write_text(content, encoding="utf-8")
    log.info(f"Informe guardado: {filename.name}")
    return filename

# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    if not (GEMINI_API_KEY or OPENAI_API_KEY or ANTHROPIC_API_KEY):
        log.error("No se ha configurado ninguna API_KEY.")
        log.error("Obtén tu clave en: https://aistudio.google.com/app/apikey")
        sys.exit(1)

    today     = datetime.now()
    desde     = today - timedelta(days=DATE_WINDOW)
    today_str = date_to_spanish(today)
    desde_str = desde.strftime("%d/%m/%Y")

    log.info("=" * 65)
    log.info(f"  RockPress Tracker · {today_str}")
    log.info(f"  Ventana: {desde_str} → {today.strftime('%d/%m/%Y')}")
    log.info(f"  Modelo: {AI_MODEL}")
    log.info("=" * 65)

    # 1. Cargar medios
    media = load_media(BBDD_FILE)
    if not media:
        log.error("Sin medios en bbddMedios.md. Abortando.")
        sys.exit(1)

    # 2. Cargar memoria
    seen_urls = load_seen_urls()

    # 3. Recopilar artículos estructurados
    collected = collect_articles(media)
    total_arts = sum(len(v) for v in collected.values())
    if total_arts == 0:
        log.error("Sin artículos extraídos. Abortando.")
        sys.exit(1)

    # 4. Construir lista compacta para Gemini (excluyendo ya vistos)
    article_list_text, total_nuevos, total_vistos = build_article_list(collected, seen_urls)

    if total_nuevos == 0:
        log.info("No hay artículos nuevos — todos ya fueron reportados anteriormente.")
        report = (
            f"# INFORME DIARIO – ROCK NACIONAL\n"
            f"Fecha: {today_str}\n\n"
            f"No se han encontrado noticias nuevas en los medios consultados "
            f"(todos los artículos de los últimos {DATE_WINDOW} días ya fueron reportados).\n"
        )
    else:
        medios_sin         = [m for m, arts in collected.items() if not arts]
        medios_con_actividad = [
            m for m, arts in collected.items()
            if any(a["url"] not in seen_urls for a in arts)
        ]
        log.info(f"Clasificando {total_nuevos} artículos nuevos en lotes de {BATCH_SIZE}…")

        try:
            report = classify_and_report(
                collected, seen_urls, total_vistos,
                medios_sin, medios_con_actividad,
                today_str, desde_str,
            )
        except Exception as e:
            log.error(f"Error con Gemini: {e}")
            sys.exit(1)

    # 5. Guardar informe
    path = save_report(report, today)

    # 6. Actualizar memoria
    new_urls  = extract_urls_from_report(report)
    today_date = today.strftime("%Y-%m-%d")
    # También registrar todos los artículos extraídos (no solo los que salieron en el informe)
    all_extracted_urls = [a["url"] for arts in collected.values() for a in arts]
    for url in all_extracted_urls + new_urls:
        seen_urls[url] = today_date
    save_seen_urls(seen_urls)

    # 7. Enviar email
    if SEND_EMAIL_FLAG:
        subject = f"🎸 RockPress – Rock Nacional {today.strftime('%d/%m/%Y')}"
        html    = markdown_to_html(report)
        send_email(subject, html, report)

    log.info("=" * 65)
    log.info(f"✅ Completado → {path.name} | {total_nuevos} artículos clasificados")
    log.info("=" * 65)
    print("\n" + report)


if __name__ == "__main__":
    main()
