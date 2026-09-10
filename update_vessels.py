#!/usr/bin/env python3
"""
update_vessels.py
Grupo Transoceánica — Prototipo Monitor de Naves EC
─────────────────────────────────────────────────────
Descarga el PDF de AMC (CMA-CGM Ecuador) y el Google Sheet de ONE,
parsea ambas fuentes y actualiza data.json en GitHub Pages.

Corre 3x/día vía Windows Task Scheduler.
Requisitos: pip install requests pdfplumber

PROTOTIPO EXPLORATORIO — ver README.md antes de usar en producción.
"""

import os
import requests
import json
import base64
import csv
import io
import re
import sys
import logging
from datetime import datetime
from pathlib import Path

# ── pip install pdfplumber ──────────────────────────────
try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False
    print("AVISO: pdfplumber no instalado. Ejecuta: pip install pdfplumber")

# ════════════════════════════════════════════════════════
#  CONFIGURACIÓN  (editar antes de usar)
# ════════════════════════════════════════════════════════

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # Personal Access Token de GitHub (variable de entorno)
GITHUB_REPO  = "andersonponce4-netizen/vessel-monitor"
GITHUB_FILE  = "data.json"

# URL pública del PDF de AMC (sin login requerido)
AMC_PDF_URL  = (
    "https://ec.cargoamc.com/LinkClick.aspx"
    "?fileticket=cYUyrp3IjlA%3d&tabid=172&portalid=1"
)

# Google Sheet ONE — exportación CSV pública
ONE_SHEET_ID  = "18vsHilT2cRxeMY3HamN63BGcSBTakEiHzgqB5vtdNWg"
ONE_SHEET_GID = "28461025"

# Directorio local para guardar copias de las fuentes
CACHE_DIR = Path(__file__).parent / "cache"

# ════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "update.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════
#  DESCARGA DE FUENTES
# ════════════════════════════════════════════════════════

def download_amc_pdf() -> bytes | None:
    """Descarga el PDF de AMC y retorna el contenido en bytes."""
    log.info("Descargando PDF AMC...")
    try:
        # Primero obtenemos la URL final (AMC usa redirect desde LinkClick)
        r = requests.get(AMC_PDF_URL, timeout=30, allow_redirects=True)
        r.raise_for_status()
        if "application/pdf" not in r.headers.get("content-type", ""):
            log.warning("La respuesta de AMC no es un PDF. Content-Type: %s",
                        r.headers.get("content-type"))
            return None
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path = CACHE_DIR / f"amc_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        cache_path.write_bytes(r.content)
        log.info("PDF AMC descargado: %s (%d KB)", cache_path.name, len(r.content) // 1024)
        return r.content
    except Exception as e:
        log.error("Error descargando PDF AMC: %s", e)
        return None


def download_one_sheet() -> list[list[str]] | None:
    """
    Descarga el Google Sheet de ONE como CSV.
    Retorna lista de filas (cada fila = lista de celdas).
    El sheet usa una matriz visual compleja — se devuelven filas brutas,
    NO se usa DictReader (los encabezados no son la primera fila).
    """
    log.info("Descargando Google Sheet ONE...")
    url = (
        f"https://docs.google.com/spreadsheets/d/{ONE_SHEET_ID}"
        f"/export?format=csv&gid={ONE_SHEET_GID}"
    )
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")

        CACHE_DIR.mkdir(exist_ok=True)
        cache_path = CACHE_DIR / f"one_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        cache_path.write_text(text, encoding="utf-8")

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        log.info("ONE Sheet: %d filas brutas descargadas", len(rows))
        return rows
    except Exception as e:
        log.error("Error descargando ONE Sheet: %s", e)
        return None

# ════════════════════════════════════════════════════════
#  PARSEO — GOOGLE SHEET ONE
# ════════════════════════════════════════════════════════

# Mapa de nombres de columna esperados en el Sheet de ONE
# Ajustar si ONE cambia los encabezados
ONE_COL_MAP = {
    "nave":      ["NAVE", "VESSEL", "Nave", "Vessel"],
    "viaje":     ["VIAJE", "VOYAGE", "Viaje", "Voyage"],
    "svc":       ["SERVICIO", "SERVICE", "Servicio", "Service"],
    "terminal":  ["TERMINAL", "Terminal"],
    "arribo":    ["ARRIBO EC", "ETA EC", "Arribo EC", "Arribo"],
    "zarpe":     ["ZARPE EC", "ETS EC", "Zarpe EC", "Zarpe"],
    "co_dry":    ["CO DRY", "CUTOFF DRY", "Cut Off Dry", "Cutoff Carga Seca"],
    "co_rf":     ["CO RF", "CO REEFER", "CUTOFF RF", "Cutoff Reefer"],
    "semana":    ["SEMANA", "WEEK", "Semana", "Week"],
}

def find_col(row: dict, candidates: list[str]) -> str | None:
    """Busca el primer encabezado que coincida (case-insensitive)."""
    keys_lower = {k.lower(): k for k in row.keys()}
    for c in candidates:
        if c.lower() in keys_lower:
            return row[keys_lower[c.lower()]]
    return None


def parse_one_date(val: str | None) -> str:
    """Normaliza fecha del Sheet ONE a formato dd/Mes HH:MM."""
    if not val or val.strip() in ("", "—", "-", "N/A", "POR CONFIRMAR"):
        return "POR CONFIRMAR"
    val = val.strip()
    # Intentar parsear formatos comunes: "8/9/2026", "8/9/2026 13:00", "8-sep-26"
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            dt = datetime.strptime(val.split(" ")[0], fmt.split(" ")[0])
            hora = val.split(" ")[1] if " " in val else "00:00"
            meses = ["Ene","Feb","Mar","Abr","May","Jun",
                     "Jul","Ago","Sep","Oct","Nov","Dic"]
            return f"{dt.day:02d}/{meses[dt.month-1]} {hora}"
        except ValueError:
            continue
    return val  # devolver tal cual si no se pudo parsear


# ── Helpers internos para parseo de matriz ONE ────────────────────────────────

def _is_date_val(s: str) -> bool:
    """Detecta si una celda es una fecha d/m/yyyy."""
    return bool(re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', (s or '').strip()))

def _is_time_val(s: str) -> bool:
    """Detecta si una celda es una hora h:mm o h:mm:ss."""
    return bool(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', (s or '').strip()))

def _is_omision_one(s: str) -> bool:
    su = (s or '').upper()
    return 'OMISI' in su or 'BLANK' in su

def _pad_row(row: list, n: int = 28) -> list:
    """Extiende una fila a n columnas con cadenas vacías."""
    return (list(row) + [''] * n)[:n]

def _fmt_one_date(day: str, date: str, time: str) -> str:
    """
    Combina día-de-semana, fecha y hora en cadena legible.
    Ej: "Domingo 13/09/2026 20:00"
    Retorna "—" si hay omisión, "POR CONFIRMAR" si no hay fecha.
    """
    day  = (day  or '').strip()
    date = (date or '').strip()
    time = (time or '').strip()

    if _is_omision_one(day) or _is_omision_one(date):
        return '—'

    parts = []
    if day and not _is_date_val(day) and not _is_time_val(day):
        parts.append(day.capitalize())
    if date and _is_date_val(date):
        try:
            d, m, y = date.split('/')
            parts.append(f"{int(d):02d}/{int(m):02d}/{y}")
        except Exception:
            parts.append(date)
    if time and _is_time_val(time):
        t = time.split(':')
        parts.append(f"{t[0]}:{t[1]}")

    return ' '.join(parts) if parts else 'POR CONFIRMAR'

def _is_past_one(date_str: str) -> bool:
    """Evalúa si una fecha en formato ONE ('Domingo 13/09/2026 20:00') ya pasó."""
    if not date_str or date_str in ('—', 'POR CONFIRMAR', ''):
        return False
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?', date_str)
    if m:
        try:
            day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hr = int(m.group(4)) if m.group(4) else 0
            mi = int(m.group(5)) if m.group(5) else 0
            return datetime(yr, mon, day, hr, mi) < datetime.now()
        except Exception:
            return False
    return False

# ──────────────────────────────────────────────────────────────────────────────

def parse_one_sheet(raw_rows: list[list[str]]) -> list[dict]:
    """
    Parsea el Google Sheet de ONE Ecuador (formato matriz visual).

    Estructura del CSV exportado:
    ┌─────────────────────────────────────────────────────────────────┐
    │ Fila 0   : vacía                                                │
    │ Fila 1   : col[1]="WEEK 37"  col[15~]="WEEK 38"                │
    │ Filas 2-12: branding/logo, vacías                               │
    │ Fila 13  : encabezados — col[2]=SERVICIO  col[3]=NAVE/VIAJE    │
    │ Filas 14+: bloques de 3 filas por nave:                         │
    │   Fila A: SERVICIO, NAVE/VIAJE, TERMINAL + día-semana en dates │
    │   Fila B: fechas (dd/m/yyyy) en columnas de fecha              │
    │   Fila C: horas  (h:mm:ss)   en columnas de fecha              │
    └─────────────────────────────────────────────────────────────────┘

    Columnas W37 (0-based): 2=SVC 3=NAVE 4=TERM 5=ARRIBO 6=ZARPE
                             7=CO_DRY 8=CO_RF
    Columnas W38 (0-based): 18=SVC 19=NAVE 20=TERM 21=ARRIBO 22=ZARPE
                             23=CO_DRY 24=CO_RF

    REGLAS DE NEGOCIO:
    - "Omisión de Recalada" en col 5 → nave omitida (W37)
    - "Blank Sailing"       en col 19 → nave omitida (W38)
    - Col 3 vacía pero col 19 con nave → entrada solo W38;
      hereda SERVICIO del último bloque W38 conocido.
    """
    vessels  = []
    vessel_id = 1000

    rows = [_pad_row(r) for r in raw_rows]

    # ── Detectar números de semana (fila 1, índice 1) ─────────────────
    wk37 = wk38 = 0
    if len(rows) > 1:
        for j, cell in enumerate(rows[1]):
            m = re.search(r'WEEK\s*(\d+)', cell.strip(), re.IGNORECASE)
            if m:
                wk_num = int(m.group(1))
                if j < 15:
                    wk37 = wk_num
                else:
                    wk38 = wk_num
    log.info("ONE: semanas detectadas W37=%s, W38=%s", wk37, wk38)

    # ── Encontrar fila de encabezados ─────────────────────────────────
    header_idx = None
    for i, row in enumerate(rows):
        if 'SERVICIO' in row[2].upper() and 'NAVE' in row[3].upper():
            header_idx = i
            break
    if header_idx is None:
        log.warning("ONE: no se encontró fila de encabezados (SERVICIO/NAVE)")
        return []
    log.info("ONE: encabezados en fila %d", header_idx)

    # ── Procesar bloques de datos ─────────────────────────────────────
    data = rows[header_idx + 1:]
    current_svc_37 = ''
    current_svc_38 = ''
    i = 0

    while i < len(data):
        row = data[i]

        if all(c == '' for c in row):
            i += 1
            continue

        nave37 = row[3].strip()
        nave38 = row[19].strip()

        is_v37 = nave37 and not _is_date_val(nave37) and not _is_time_val(nave37)
        is_v38 = nave38 and not _is_date_val(nave38) and not _is_time_val(nave38)

        if not is_v37 and not is_v38:
            i += 1
            continue

        # Fila A — información de nave
        vessel_row = row
        if row[2].strip():
            current_svc_37 = row[2].strip()
        if row[18].strip():
            current_svc_38 = row[18].strip()
        term37 = row[4].strip()
        term38 = row[20].strip()

        # Buscar filas B (fechas) y C (horas) a continuación
        date_row = _pad_row([])
        time_row = _pad_row([])
        j = i + 1
        while j < len(data) and j <= i + 3:
            nr = data[j]
            # Parar si es otra fila de nave
            if ((nr[3].strip() and not _is_date_val(nr[3]) and not _is_time_val(nr[3])) or
                    (nr[19].strip() and not _is_date_val(nr[19]) and not _is_time_val(nr[19]))):
                break
            has_date = _is_date_val(nr[5]) or _is_date_val(nr[21])
            has_time = _is_time_val(nr[5]) or _is_time_val(nr[21])
            if has_date:
                date_row = _pad_row(nr)
                j += 1
                if j < len(data) and (_is_time_val(data[j][5]) or _is_time_val(data[j][21])):
                    time_row = _pad_row(data[j])
                    j += 1
                break
            elif has_time:
                time_row = _pad_row(nr)
                j += 1
                break
            j += 1
        i = j

        def _build_vessel(wk, svc, nave, term, c_arr, c_zar, c_dry, c_rf):
            """Construye el dict de nave para el dashboard."""
            if _is_omision_one(vessel_row[c_arr]):
                a = z = d = rf = '—'
                st, nota = 'skip', 'Omisión de recalada confirmada por ONE.'
            elif _is_omision_one(nave):
                a = z = d = rf = '—'
                nave = f'{svc} Blank Sailing'
                st, nota = 'skip', 'Blank Sailing.'
            else:
                a  = _fmt_one_date(vessel_row[c_arr], date_row[c_arr], time_row[c_arr])
                z  = _fmt_one_date(vessel_row[c_zar], date_row[c_zar], time_row[c_zar])
                d  = _fmt_one_date(vessel_row[c_dry], date_row[c_dry], time_row[c_dry])
                rf = _fmt_one_date(vessel_row[c_rf],  date_row[c_rf],  time_row[c_rf])
                st = 'pend' if 'CONFIRMAR' in (d + rf) else 'ok'
                nota = None

            vm = re.search(r'\s+(\d{4}[EWNS]?)$', nave)
            viaje = vm.group(1) if vm else ''

            return {
                'id':          vessel_id,
                'wk':          wk,
                'carrier':     detect_carrier_one(nave, svc),
                'nave':        nave,
                'viaje':       viaje,
                'terminal':    normalize_terminal(term),
                'svc':         svc,
                'dest':        dest_from_svc(svc),
                'arribo':      a,
                'zarpe':       z,
                'zarpe_prev':  None,
                'co_dry':      d,
                'co_dry_prev': None,
                'co_dry_past': _is_past_one(d),
                'co_rf':       rf,
                'co_rf_prev':  None,
                'co_rf_past':  _is_past_one(rf),
                'mrn':         '—',
                'fuente':      'ONE',
                'status':      st,
                'nota':        nota,
            }

        if is_v37:
            vessels.append(_build_vessel(wk37, current_svc_37, nave37, term37,
                                         5, 6, 7, 8))
            vessel_id += 1
        if is_v38:
            vessels.append(_build_vessel(wk38, current_svc_38, nave38, term38,
                                         21, 22, 23, 24))
            vessel_id += 1

    log.info("ONE Sheet parseado: %d naves", len(vessels))
    return vessels


def detect_carrier_one(nave: str, svc: str) -> str:
    nave_u = nave.upper()
    svc_u  = (svc or "").upper()
    if "CMA" in nave_u or "CGM" in nave_u:  return "CMA"
    if "ONE" in nave_u:                      return "ONE"
    if "COSCO" in nave_u or "OOCL" in nave_u: return "COS"
    if "HMM" in nave_u:                     return "HMM"
    if "HAPAG" in nave_u:                   return "HLC"
    if "ECO" in nave_u:                     return "XPD"
    if "AX3" in svc_u or "AX4" in svc_u:   return "ONE"
    if "FLX" in svc_u:                      return "ONE"
    return "ONE"  # default para sheet de ONE


def normalize_terminal(t: str | None) -> str:
    if not t: return "—"
    t = t.strip().upper()
    if "TPG" in t:       return "Guayaquil (TPG)"
    if "DPW" in t or "POSORJA" in t: return "Posorja (DPW)"
    if "CONTECON" in t or "CGSA" in t: return "Guayaquil (CONTECON)"
    return t.title()


def dest_from_svc(svc: str | None) -> str:
    if not svc: return "—"
    svc_u = svc.upper()
    if "FLX-SUR" in svc_u or "AMERICAS XL SB" in svc_u:
        return "EEUU, Puerto Rico, Bahamas"
    if "FLX-NORTE" in svc_u or "AMERICAS XL NB" in svc_u:
        return "EEUU, Puerto Rico, México"
    if "EUROSAL SB" in svc_u:  return "España, Francia, Italia, Mediterráneo Sur"
    if "EUROSAL NB" in svc_u:  return "Bélgica, Bilbao, Rotterdam, Norte Europa"
    if "MEDCAR" in svc_u:      return "Mediterráneo / Caribe"
    if "ACSA" in svc_u:        return "China, Japón, México"
    if "GPX-SUR" in svc_u:    return "Asia / Transpacífico Sur"
    if "GPX-NORTE" in svc_u:  return "Asia / Transpacífico Norte"
    if "AX3" in svc_u or "AX4" in svc_u: return "Asia / Transpacífico"
    if "M2A" in svc_u:        return "China, Japón, México"
    return svc


def is_past(date_str: str) -> bool:
    """Retorna True si la fecha ya pasó respecto a hoy."""
    if date_str in ("POR CONFIRMAR", "—", ""):
        return False
    now = datetime.now()
    meses = {"Ene":1,"Feb":2,"Mar":3,"Abr":4,"May":5,"Jun":6,
              "Jul":7,"Ago":8,"Sep":9,"Oct":10,"Nov":11,"Dic":12}
    m = re.match(r"(\d{1,2})/(\w{3})\s+(\d{2}:\d{2})", date_str)
    if m:
        day, mes, hora = int(m.group(1)), m.group(2), m.group(3)
        mon = meses.get(mes, now.month)
        yr = now.year if mon >= now.month else now.year + 1
        try:
            h, mi = map(int, hora.split(":"))
            dt = datetime(yr, mon, day, h, mi)
            return dt < now
        except Exception:
            return False
    return False

# ════════════════════════════════════════════════════════
#  PARSEO — PDF AMC
# ════════════════════════════════════════════════════════

# Carriers identificados por prefijo de MRN o nombre de nave
AMC_CARRIER_MAP = {
    "CMAU": "CMA", "CMAECUADOR": "CMA", "CMA": "CMA",
    "ONEU": "ONE", "COSU": "COS", "OOLU": "COS",
    "HLCU": "HLC", "HLAG": "HLC",
    "XPDF": "XPD", "XPRESS": "XPD",
    "HMMM": "HMM",
}

def carrier_from_mrn(mrn: str) -> str:
    if not mrn: return "—"
    for prefix, carrier in AMC_CARRIER_MAP.items():
        if prefix in mrn.upper():
            return carrier
    return "—"

def carrier_from_name(name: str) -> str:
    name_u = name.upper()
    if "CMA CGM" in name_u or "APL" in name_u:  return "CMA"
    if "COSCO" in name_u or "OOCL" in name_u:    return "COS"
    if "ONE " in name_u or name_u.startswith("ONE"): return "ONE"
    if "HAPAG" in name_u or "LLOYD" in name_u:   return "HLC"
    if "ECO " in name_u:                          return "XPD"
    if "HMM" in name_u:                           return "HMM"
    return "—"


def parse_amc_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    Extrae naves del PDF de AMC usando pdfplumber.
    El PDF tiene una tabla por servicio. Columnas típicas:
    VESSEL | VOYAGE | POL | ETA | ETB | ETS | MRN | CO DRY | CO RF
    """
    if not HAS_PDF:
        log.warning("pdfplumber no disponible — saltando parseo de PDF AMC")
        return []

    vessels = []
    now = datetime.now()
    week_now = now.isocalendar()[1]

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            current_svc = "—"
            current_wk  = week_now

            for page in pdf.pages:
                # Detectar nombre de servicio en el texto de la página
                text = page.extract_text() or ""
                for line in text.split("\n"):
                    line_u = line.upper().strip()
                    for svc_kw in ["EUROSAL SB","EUROSAL NB","AMERICAS XL SB",
                                   "AMERICAS XL NB","FLX-SUR","FLX-NORTE",
                                   "MEDCAR","ACSA 1","M2A","GPX-SUR","GPX-NORTE",
                                   "AX3","AX4"]:
                        if svc_kw in line_u:
                            current_svc = svc_kw
                    # Detectar semana
                    wk_m = re.search(r"SEMANA[:\s]+(\d{2})", line_u)
                    if wk_m:
                        current_wk = int(wk_m.group(1))

                # Extraer tabla
                tables = page.extract_tables()
                for table in tables:
                    # Necesitamos al menos: fila-título + fila-encabezados + 1 fila de datos
                    if not table or len(table) < 3:
                        continue

                    # ── Fila 0: título de semana ─────────────────────────────
                    # Ej: ['Week # 37', '07 de Septiembre al 13 de Septiembre', None, ...]
                    title_cell = str(table[0][0] or "")
                    wk_m_tbl = re.search(r"Week\s*#\s*(\d+)", title_cell, re.IGNORECASE)
                    if wk_m_tbl:
                        current_wk = int(wk_m_tbl.group(1))

                    # ── Fila 1: encabezados reales ───────────────────────────
                    # Ej: ['Vessel','Voyage','Port of Loading','ETA','ETB','ETS','MRN','Cut Off','Service']
                    headers = [str(h).upper().strip() if h else "" for h in table[1]]

                    # Índices de columnas clave (flexibles)
                    def col_idx(candidates):
                        for c in candidates:
                            for i, h in enumerate(headers):
                                if c in h:
                                    return i
                        return None

                    i_vessel = col_idx(["VESSEL","NAVE"])
                    i_voyage = col_idx(["VOYAGE","VIAJE"])
                    i_pol    = col_idx(["POL","TERMINAL","PORT OF LOADING","PORT"])
                    i_eta    = col_idx(["ETA","ARRIBO"])
                    i_ets    = col_idx(["ETS","ZARPE","ETD"])
                    i_mrn    = col_idx(["MRN","BOOKING REF"])
                    # El PDF de AMC tiene una sola columna "Cut Off" para DRY y REEFER
                    i_dry    = col_idx(["CO DRY","DRY","CARGA SECA","CUT OFF"])
                    i_rf     = col_idx(["CO RF","CO REF","REEFER","RF","CUT OFF"])
                    i_svc    = col_idx(["SERVICE","SERVICIO","SVC"])

                    if i_vessel is None:
                        log.debug("Tabla sin columna VESSEL — omitida. Headers: %s", headers)
                        continue  # no es tabla de naves

                    for row in table[2:]:
                        def cell(idx):
                            if idx is None or idx >= len(row):
                                return ""
                            return str(row[idx] or "").strip()

                        nave = cell(i_vessel)
                        if not nave or nave.upper() in ("VESSEL","NAVE",""):
                            continue

                        mrn     = cell(i_mrn)
                        carrier = carrier_from_mrn(mrn) or carrier_from_name(nave)
                        co_dry  = cell(i_dry) or "POR CONFIRMAR"
                        co_rf   = cell(i_rf)  or "POR CONFIRMAR"
                        zarpe   = cell(i_ets) or "POR CONFIRMAR"
                        arribo  = cell(i_eta) or "—"
                        term    = normalize_terminal(cell(i_pol))

                        # Usar servicio de la columna SERVICE si está disponible,
                        # si no, conservar el servicio detectado en el texto de la página
                        row_svc = cell(i_svc).strip() if i_svc is not None else ""
                        if row_svc:
                            current_svc = row_svc

                        status = "pend" if "CONFIRMAR" in (co_dry+co_rf+zarpe).upper() else "ok"

                        vessels.append({
                            "id":          2000 + len(vessels),
                            "wk":          current_wk,
                            "carrier":     carrier,
                            "nave":        nave,
                            "viaje":       cell(i_voyage),
                            "terminal":    term,
                            "svc":         current_svc,
                            "dest":        dest_from_svc(current_svc),
                            "arribo":      arribo,
                            "zarpe":       zarpe,
                            "zarpe_prev":  None,
                            "co_dry":      co_dry,
                            "co_dry_prev": None,
                            "co_dry_past": is_past(co_dry),
                            "co_rf":       co_rf,
                            "co_rf_prev":  None,
                            "co_rf_past":  is_past(co_rf),
                            "mrn":         mrn or "—",
                            "fuente":      "AMC",
                            "status":      status,
                            "nota":        None,
                        })

    except Exception as e:
        log.error("Error parseando PDF AMC: %s", e)

    log.info("PDF AMC parseado: %d naves", len(vessels))
    return vessels

# ════════════════════════════════════════════════════════
#  MERGE: combinar AMC + ONE sin duplicados
# ════════════════════════════════════════════════════════

def merge_vessels(amc: list[dict], one: list[dict]) -> list[dict]:
    """
    Une ambas fuentes. Si una nave aparece en los dos, se toma la de ONE
    para las fechas (más actualizada) y se marca fuente 'AMC+ONE'.
    Criterio de deduplicación: nombre de nave similar + semana.
    """
    merged = list(amc)  # empezar con AMC como base

    for v_one in one:
        # Buscar si ya existe en AMC (por nombre, ignorando mayúsculas y espacios)
        name_clean = re.sub(r"\s+", " ", v_one["nave"].upper().strip())
        found = False
        for i, v_amc in enumerate(merged):
            amc_clean = re.sub(r"\s+", " ", v_amc["nave"].upper().strip())
            if name_clean == amc_clean and v_one["wk"] == v_amc["wk"]:
                # Actualizar con datos de ONE (más confiables para fechas)
                merged[i]["zarpe"]       = v_one["zarpe"]
                merged[i]["arribo"]      = v_one["arribo"]
                merged[i]["co_dry"]      = v_one["co_dry"]
                merged[i]["co_rf"]       = v_one["co_rf"]
                merged[i]["co_dry_past"] = v_one["co_dry_past"]
                merged[i]["co_rf_past"]  = v_one["co_rf_past"]
                merged[i]["fuente"]      = "AMC+ONE"
                if merged[i]["status"] == "pend" and v_one["status"] == "ok":
                    merged[i]["status"] = "ok"
                    merged[i]["nota"] = "Fechas confirmadas por Google Sheet ONE."
                found = True
                break
        if not found:
            merged.append(v_one)

    # Ordenar: semana asc, luego zarpe asc
    merged.sort(key=lambda v: (v["wk"], v["zarpe"] or "ZZZ"))

    # Re-numerar IDs
    for i, v in enumerate(merged):
        v["id"] = i + 1

    return merged

# ════════════════════════════════════════════════════════
#  PUBLICAR EN GITHUB
# ════════════════════════════════════════════════════════

def push_to_github(data: list[dict]) -> bool:
    """Sube data.json al repositorio GitHub via API."""
    if GITHUB_TOKEN == "TU_TOKEN_AQUI":
        log.error("GITHUB_TOKEN no configurado. Edita update_vessels.py.")
        return False

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"

    # Obtener SHA del archivo actual (necesario para actualizarlo)
    sha = None
    r = requests.get(api_url, headers=headers, timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code != 404:
        log.error("Error leyendo GitHub: %s %s", r.status_code, r.text[:200])
        return False

    payload_str = json.dumps({
        "vessels": data,
        "updated_at": datetime.now().isoformat(),
        "sources": {
            "amc_url": AMC_PDF_URL,
            "one_sheet": f"https://docs.google.com/spreadsheets/d/{ONE_SHEET_ID}",
        }
    }, ensure_ascii=False, indent=2)

    body = {
        "message": f"auto-update {datetime.now().strftime('%Y-%m-%d %H:%M')} EC",
        "content": base64.b64encode(payload_str.encode("utf-8")).decode(),
    }
    if sha:
        body["sha"] = sha

    r = requests.put(api_url, headers=headers, json=body, timeout=15)
    if r.status_code in (200, 201):
        log.info("data.json actualizado en GitHub (%d naves)", len(data))
        return True
    else:
        log.error("Error subiendo a GitHub: %s %s", r.status_code, r.text[:300])
        return False

# ════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════

def main():
    log.info("═══ Inicio actualización %s ═══", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # 1. Descargar fuentes
    pdf_bytes = download_amc_pdf()
    one_rows  = download_one_sheet()

    # 2. Parsear
    amc_vessels = parse_amc_pdf(pdf_bytes) if pdf_bytes else []
    one_vessels = parse_one_sheet(one_rows) if one_rows else []

    if not amc_vessels and not one_vessels:
        log.error("No se obtuvo datos de ninguna fuente. Abortando.")
        sys.exit(1)

    # 3. Merge
    vessels = merge_vessels(amc_vessels, one_vessels)
    log.info("Total naves tras merge: %d", len(vessels))

    # 4. Guardar copia local
    CACHE_DIR.mkdir(exist_ok=True)
    local_json = CACHE_DIR / "data_latest.json"
    local_json.write_text(
        json.dumps({"vessels": vessels, "updated_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info("Copia local guardada: %s", local_json)

    # 5. Publicar en GitHub
    ok = push_to_github(vessels)
    if ok:
        log.info("═══ Actualización completada exitosamente ═══")
    else:
        log.warning("═══ Actualización con errores — revisar update.log ═══")
        sys.exit(1)


if __name__ == "__main__":
    main()
