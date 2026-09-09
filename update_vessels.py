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


def download_one_sheet() -> list[dict] | None:
    """Descarga el Google Sheet de ONE como CSV y retorna lista de filas."""
    log.info("Descargando Google Sheet ONE...")
    url = (
        f"https://docs.google.com/spreadsheets/d/{ONE_SHEET_ID}"
        f"/export?format=csv&gid={ONE_SHEET_GID}"
    )
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        log.info("ONE Sheet: %d filas descargadas", len(rows))
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path = CACHE_DIR / f"one_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        cache_path.write_text(text, encoding="utf-8")
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


def parse_one_sheet(rows: list[dict]) -> list[dict]:
    """Convierte filas del CSV de ONE a la estructura del dashboard."""
    vessels = []
    # Detectar semana actual y siguiente desde la fecha de hoy
    now = datetime.now()
    week_now = now.isocalendar()[1]

    for i, row in enumerate(rows):
        nave    = find_col(row, ONE_COL_MAP["nave"])
        viaje   = find_col(row, ONE_COL_MAP["viaje"])
        svc     = find_col(row, ONE_COL_MAP["svc"])
        term    = find_col(row, ONE_COL_MAP["terminal"])
        arribo  = find_col(row, ONE_COL_MAP["arribo"])
        zarpe   = find_col(row, ONE_COL_MAP["zarpe"])
        co_dry  = find_col(row, ONE_COL_MAP["co_dry"])
        co_rf   = find_col(row, ONE_COL_MAP["co_rf"])
        semana  = find_col(row, ONE_COL_MAP["semana"])

        if not nave or not nave.strip():
            continue  # saltar filas vacías / encabezados intermedios

        # Determinar número de semana
        wk = int(semana) if semana and semana.strip().isdigit() else week_now

        # Detectar omisión de recalada
        is_skip = any(kw in str(arribo).upper() for kw in
                      ["OMISION", "OMISIÓN", "BLANK", "SKIP", "NO ESCALA"])

        arribo_fmt = "—" if is_skip else parse_one_date(arribo)
        zarpe_fmt  = "—" if is_skip else parse_one_date(zarpe)
        co_dry_fmt = "—" if is_skip else parse_one_date(co_dry)
        co_rf_fmt  = "—" if is_skip else parse_one_date(co_rf)

        status = "skip" if is_skip else (
            "pend" if "POR CONFIRMAR" in (co_dry_fmt + co_rf_fmt) else "ok"
        )

        # Detectar carrier por nombre de nave o servicio
        carrier = detect_carrier_one(nave, svc)

        # Detectar si cut-off ya venció
        co_dry_past = is_past(co_dry_fmt)
        co_rf_past  = is_past(co_rf_fmt)

        vessels.append({
            "id":          1000 + i,
            "wk":          wk,
            "carrier":     carrier,
            "nave":        nave.strip(),
            "viaje":       (viaje or "").strip(),
            "terminal":    normalize_terminal(term),
            "svc":         (svc or "").strip(),
            "dest":        dest_from_svc(svc),
            "arribo":      arribo_fmt,
            "zarpe":       zarpe_fmt,
            "zarpe_prev":  None,
            "co_dry":      co_dry_fmt,
            "co_dry_prev": None,
            "co_dry_past": co_dry_past,
            "co_rf":       co_rf_fmt,
            "co_rf_prev":  None,
            "co_rf_past":  co_rf_past,
            "mrn":         "—",
            "fuente":      "ONE",
            "status":      status,
            "nota":        "Omisión de recalada confirmada por ONE." if is_skip else None,
        })

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
                    if not table or len(table) < 2:
                        continue
                    # Primera fila como encabezados
                    headers = [str(h).upper().strip() if h else "" for h in table[0]]

                    # Índices de columnas clave (flexibles)
                    def col_idx(candidates):
                        for c in candidates:
                            for i, h in enumerate(headers):
                                if c in h:
                                    return i
                        return None

                    i_vessel = col_idx(["VESSEL","NAVE"])
                    i_voyage = col_idx(["VOYAGE","VIAJE"])
                    i_pol    = col_idx(["POL","TERMINAL","PORT"])
                    i_eta    = col_idx(["ETA","ARRIBO"])
                    i_ets    = col_idx(["ETS","ZARPE","ETD"])
                    i_mrn    = col_idx(["MRN","BOOKING REF"])
                    i_dry    = col_idx(["DRY","CO DRY","CARGA SECA"])
                    i_rf     = col_idx(["RF","REEFER","CO RF","CO REF"])

                    if i_vessel is None:
                        continue  # no es tabla de naves

                    for row in table[1:]:
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
