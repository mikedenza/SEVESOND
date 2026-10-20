#!/usr/bin/env python3
"""
Sonda di accessibilità dati per il monitoraggio del Seveso (Niguarda, Milano).

Non fa previsioni e non manda avvisi: misura soltanto, a ogni esecuzione,
- se le fonti rispondono a un client automatico (status HTTP, errori, tempi);
- con quale RITARDO arrivano i dati (ora di pubblicazione vs ora attuale).

Fonti:
  1. ARPA Lombardia via dati.lombardia.it (Socrata): idrometri e pluviometri
  2. Radar Protezione Civile (radar-api.protezionecivile.it)
  3. Open-Meteo, modello ICON-2I di ItaliaMeteo

Solo libreria standard, Python >= 3.10.
Output: data/probe_log.jsonl (una riga per esecuzione), data/sensors.json,
        data/latest.json (ultimi livelli letti).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
SENSORS_FILE = DATA / "sensors.json"
LOG_FILE = DATA / "probe_log.jsonl"
LATEST_FILE = DATA / "latest.json"

SOCRATA = "https://www.dati.lombardia.it/resource"
DS_STATIONS = "nf78-nj6b"   # anagrafica stazioni/sensori idro-nivo-meteo
DS_CURRENT = "647i-nhxk"    # dati sensori, flusso corrente
RADAR_API = "https://radar-api.protezionecivile.it"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OM_MODEL = "italia_meteo_arpae_icon_2i"

UA = "seveso-probe/0.1 (progetto personale non commerciale di monitoraggio)"

# Riquadro che contiene il bacino del Seveso, dalla Brianza comasca a Niguarda.
BBOX = {"lat_min": 45.49, "lat_max": 45.85, "lng_min": 9.00, "lng_max": 9.25}

# Punti per le previsioni: valle (Niguarda) e bacino a monte.
OM_POINTS = {
    "Niguarda": (45.517, 9.190),
    "Cesano Maderno": (45.630, 9.150),
    "Seveso": (45.648, 9.140),
    "Cantu": (45.740, 9.130),
}

# ARPA pubblica gli orari in ora solare (UTC+1) tutto l'anno.
ORA_SOLARE = timezone(timedelta(hours=1))
SENSORS_MAX_AGE_H = 24
ARPA_WINDOW_H = 6   # finestra di dati recenti richiesta a ogni esecuzione


# ---------------------------------------------------------------- HTTP

def http(url: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None, timeout: int = 30) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return {"ok": True, "status": r.status, "bytes": len(raw),
                    "ms": round((time.monotonic() - t0) * 1000), "body": raw}
    except urllib.error.HTTPError as e:
        snippet = e.read()[:300].decode("utf-8", "replace")
        keep = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                "x-ratelimit-reset", "x-socrata-requestid", "server", "via", "cf-ray")
        hdrs = {k.lower(): v for k, v in e.headers.items() if k.lower() in keep} if e.headers else {}
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}",
                "snippet": snippet, "headers": hdrs,
                "ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:  # rete, DNS, timeout, TLS...
        return {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}",
                "ms": round((time.monotonic() - t0) * 1000)}


def as_json(r: dict):
    if not r["ok"]:
        return None
    try:
        return json.loads(r["body"])
    except ValueError as e:
        r["ok"] = False
        r["error"] = f"JSON non valido: {e}"
        return None


def public(r: dict) -> dict:
    """Versione della risposta da scrivere nel log (senza il corpo)."""
    return {k: v for k, v in r.items() if k != "body"}


def socrata_auth() -> tuple[dict, str]:
    """App Token (header X-App-Token) oppure API Key (Key ID + Key Secret, Basic auth)."""
    import base64
    key_id = os.environ.get("SOCRATA_KEY_ID")
    key_secret = os.environ.get("SOCRATA_KEY_SECRET")
    if key_id and key_secret:
        cred = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}, "api_key"
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        return {"X-App-Token": token.strip()}, "app_token"
    return {}, "anonimo"


RETRY_WAITS_S = (20, 60)  # attese prima dei nuovi tentativi dopo un HTTP 429


def socrata(ds: str, params: dict, timeout: int = 30) -> tuple[dict, list | None]:
    url = f"{SOCRATA}/{ds}.json?" + urllib.parse.urlencode(params)
    headers, _ = socrata_auth()
    attempts = []
    for wait in (0, *RETRY_WAITS_S):
        if wait:
            time.sleep(wait)
        r = http(url, headers=headers, timeout=timeout)
        attempts.append(r.get("status"))
        if r.get("status") != 429:
            break
    if len(attempts) > 1:
        r["attempts"] = attempts
    rows = as_json(r)
    if rows is not None and not isinstance(rows, list):
        r["ok"], r["error"], rows = False, f"risposta inattesa: {str(rows)[:200]}", None
    return r, rows


def minutes_since(dt: datetime, now: datetime) -> float:
    return round((now - dt).total_seconds() / 60, 1)


def parse_arpa_time(s: str) -> datetime:
    # es. "2026-10-20T10:20:00.000" (ora solare, senza fuso)
    return datetime.fromisoformat(s[:19]).replace(tzinfo=ORA_SOLARE)


# ---------------------------------------------------------------- ARPA

def discover_sensors(report: dict) -> list[dict]:
    """Trova idrometri e pluviometri del bacino. Cache di 24 ore."""
    if SENSORS_FILE.exists():
        cached = json.loads(SENSORS_FILE.read_text())
        age_h = (time.time() - SENSORS_FILE.stat().st_mtime) / 3600
        if cached.get("sensors") and age_h < SENSORS_MAX_AGE_H:
            report["discovery"] = {"cached": True, "n": len(cached["sensors"])}
            return cached["sensors"]

    b = BBOX
    where = (
        f"(lat between {b['lat_min']} and {b['lat_max']} "
        f"and lng between {b['lng_min']} and {b['lng_max']} "
        "and (upper(tipologia) like '%IDROMETR%' or upper(tipologia) like '%PRECIPITAZ%'))"
        " or (upper(nomestazione) like '%SEVESO%' and upper(tipologia) like '%IDROMETR%')"
    )
    r, rows = socrata(DS_STATIONS, {"$where": where, "$limit": "500"})
    report["discovery"] = public(r)

    if rows is None:
        # Probabile schema diverso dal previsto: registra un campione per adattare lo script.
        r2, sample = socrata(DS_STATIONS, {"$limit": "2"})
        report["discovery"]["sample_request"] = public(r2)
        report["discovery"]["sample_rows"] = sample
        return []

    sensors = []
    for row in rows:
        if row.get("datastop") or str(row.get("storico", "")).upper() == "S":  # sensore dismesso
            continue
        tipo = row.get("tipologia", "")
        sensors.append({
            "idsensore": str(row.get("idsensore")),
            "tipo": "livello" if "IDROMETR" in tipo.upper() else "pioggia",
            "tipologia": tipo,
            "stazione": row.get("nomestazione"),
            "unita": row.get("unit_dimisura"),
            "lat": row.get("lat"),
            "lng": row.get("lng"),
            "nome_contiene_seveso": "SEVESO" in str(row.get("nomestazione", "")).upper(),
        })
    report["discovery"]["n"] = len(sensors)
    report["discovery"]["fields"] = sorted(rows[0].keys()) if rows else []
    SENSORS_FILE.write_text(json.dumps(
        {"updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "bbox": BBOX, "sensors": sensors}, indent=1, ensure_ascii=False))
    return sensors


def probe_arpa(sensors: list[dict], now: datetime, report: dict) -> None:
    out = {}
    report["arpa"] = out
    if not sensors:
        out["error"] = "nessun sensore trovato (vedi discovery)"
        return

    # Il dataset "corrente" contiene anni di misure: un'aggregazione su tutto
    # va in timeout. Si chiede solo una finestra recente, sfruttando l'indice
    # sulla data, e l'ultimo dato per sensore si calcola qui.
    ids = ",".join(f"'{s['idsensore']}'" for s in sensors)
    since = (now.astimezone(ORA_SOLARE) - timedelta(hours=ARPA_WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%S")
    r, rows = socrata(DS_CURRENT, {
        "$select": "idsensore, data, valore, stato",
        "$where": f"data > '{since}' and idsensore in ({ids})",
        "$order": "data DESC",
        "$limit": "10000",
    }, timeout=90)
    out["request"] = public(r)
    out["window_h"] = ARPA_WINDOW_H
    if rows is None:
        if r.get("status") == 429:
            return
        r2, sample = socrata(DS_CURRENT, {"$limit": "2", "$order": ":id"})
        out["sample_request"] = public(r2)
        out["sample_rows"] = sample
        return
    out["rows"] = len(rows)

    by_id = {s["idsensore"]: s for s in sensors}
    series: dict[str, list] = {}
    for row in rows:
        series.setdefault(str(row.get("idsensore")), []).append(row)

    lags = {}
    for sid, pts in series.items():
        valid = [p for p in pts if p.get("valore") not in (None, "-9999", "-9999.0")]
        try:
            if valid:
                lags[sid] = minutes_since(max(parse_arpa_time(p["data"]) for p in valid), now)
        except (KeyError, ValueError):
            continue
    out["lag_min_per_sensor"] = lags
    out["sensors_with_data"] = len(lags)
    out["sensors_without_recent_data"] = sorted(
        f"{s['idsensore']} {s['stazione']} ({s['tipo']})" for s in sensors if s["idsensore"] not in lags)
    if lags:
        vals = list(lags.values())
        out["lag_min"] = {"min": min(vals), "median": statistics.median(vals), "max": max(vals)}
        level_lags = [v for k, v in lags.items() if by_id.get(k, {}).get("tipo") == "livello"]
        if level_lags:
            out["lag_min_livelli_median"] = statistics.median(level_lags)

    # Ultimi valori di idrometri e pluviometri, per controllare unità e plausibilità.
    LATEST_FILE.write_text(json.dumps({
        "read_utc": now.isoformat(timespec="seconds"),
        "note": "orari in ora solare (UTC+1); -9999 = dato mancante",
        "stations": {sid: {"stazione": by_id.get(sid, {}).get("stazione"),
                           "tipo": by_id.get(sid, {}).get("tipo"),
                           "ultimi": [{"t": p.get("data"), "v": p.get("valore"), "stato": p.get("stato")}
                                      for p in pts[:18]]}
                     for sid, pts in sorted(series.items())},
    }, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- Radar

def probe_radar(now: datetime, full: bool, report: dict) -> None:
    out = {}
    report["radar"] = out
    for product in ("SRI", "CUM3"):
        r = http(f"{RADAR_API}/findLastProductByType?type={product}")
        body = as_json(r)
        item = {"request": public(r)}
        try:
            lp = body["lastProducts"][0]
            t = datetime.fromtimestamp(lp["time"] / 1000, timezone.utc)
            item.update({"last_utc": t.isoformat(), "lag_min": minutes_since(t, now),
                         "period": lp.get("period"), "_time_ms": lp["time"]})
        except (TypeError, KeyError, IndexError, ValueError):
            if r["ok"]:
                item["error"] = f"formato inatteso: {str(body)[:200]}"
        out[product] = item

    # Test di download completo (GeoTIFF via URL S3): una volta all'ora, per non pesare.
    sri = out.get("SRI", {})
    if full and "_time_ms" in sri:
        r = http(f"{RADAR_API}/downloadProduct", method="POST",
                 body={"productType": "SRI", "productDate": sri["_time_ms"]})
        body = as_json(r)
        dl = {"request": public(r)}
        url = body.get("url") if isinstance(body, dict) else None
        if url:
            f = http(url, headers={"Accept": "*/*"}, timeout=60)
            dl["file"] = {k: v for k, v in public(f).items() if k != "snippet"}
            dl["file_host"] = urllib.parse.urlparse(url).netloc
        elif r["ok"]:
            dl["error"] = f"nessun url nella risposta: {str(body)[:200]}"
        out["download_test"] = dl
    for item in out.values():
        if isinstance(item, dict):
            item.pop("_time_ms", None)


# ---------------------------------------------------------------- Open-Meteo

def probe_openmeteo(now: datetime, report: dict) -> None:
    names = list(OM_POINTS)
    params = {
        "latitude": ",".join(str(OM_POINTS[n][0]) for n in names),
        "longitude": ",".join(str(OM_POINTS[n][1]) for n in names),
        "hourly": "precipitation",
        "forecast_days": "2",
        "timezone": "UTC",
        "models": OM_MODEL,
    }
    out = {"model": OM_MODEL}
    report["openmeteo"] = out
    r = http(OPEN_METEO + "?" + urllib.parse.urlencode(params))
    if not r["ok"] and r.get("status") == 400:
        # Nome modello cambiato o non disponibile: riprova col modello automatico.
        out["model_error"] = r.get("snippet")
        params.pop("models")
        out["model"] = "best_match (fallback)"
        r = http(OPEN_METEO + "?" + urllib.parse.urlencode(params))
    out["request"] = public(r)
    body = as_json(r)
    if body is None:
        return
    if isinstance(body, dict):
        body = [body]
    cutoff = now + timedelta(hours=24)
    totals = {}
    for name, loc in zip(names, body):
        h = loc.get("hourly", {})
        tot = 0.0
        for t, p in zip(h.get("time", []), h.get("precipitation", [])):
            ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            if now <= ts < cutoff and p is not None:
                tot += p
        totals[name] = round(tot, 1)
    out["pioggia_prevista_24h_mm"] = totals


# ---------------------------------------------------------------- main

def main() -> int:
    DATA.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    full = now.minute < 10 or bool(os.environ.get("PROBE_FULL"))
    report = {"run_utc": now.isoformat(timespec="seconds"), "full": full,
              "socrata_token": socrata_auth()[1] != "anonimo",
              "socrata_auth": socrata_auth()[1]}

    for name, fn in (
        ("arpa", lambda: probe_arpa(discover_sensors(report), now, report)),
        ("radar", lambda: probe_radar(now, full, report)),
        ("openmeteo", lambda: probe_openmeteo(now, report) if full else None),
    ):
        try:
            fn()
        except Exception as e:  # una fonte che si rompe non deve fermare le altre
            report.setdefault(name, {})["crash"] = f"{type(e).__name__}: {e}"

    with LOG_FILE.open("a") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")

    # Riepilogo leggibile nel log di GitHub Actions
    a, rd = report.get("arpa", {}), report.get("radar", {})
    print(f"Esecuzione {report['run_utc']} (completa: {full}, accesso ARPA: {report['socrata_auth']})")
    print(f"  Scoperta sensori: {report.get('discovery', {}).get('n', '?')} "
          f"(errore: {report.get('discovery', {}).get('error', 'nessuno')})")
    print(f"  ARPA: {a.get('sensors_with_data', 0)} sensori con dati recenti, "
          f"ritardo {a.get('lag_min', 'n/d')} min, errore: {a.get('request', {}).get('error', a.get('error', 'nessuno'))}")
    for p in ("SRI", "CUM3"):
        it = rd.get(p, {})
        print(f"  Radar {p}: ritardo {it.get('lag_min', 'n/d')} min, "
              f"errore: {it.get('request', {}).get('error', it.get('error', 'nessuno'))}")
    if "download_test" in rd:
        f_ = rd["download_test"].get("file", {})
        print(f"  Radar download: ok={f_.get('ok')} bytes={f_.get('bytes')} "
              f"errore: {rd['download_test'].get('request', {}).get('error', f_.get('error', 'nessuno'))}")
    if "openmeteo" in report:
        om = report["openmeteo"]
        print(f"  Open-Meteo ({om.get('model')}): {om.get('pioggia_prevista_24h_mm', 'n/d')} "
              f"errore: {om.get('request', {}).get('error', 'nessuno')}")
    if report.get("discovery", {}).get("sample_rows") or a.get("sample_rows"):
        print("  ATTENZIONE: schema diverso dal previsto, campione salvato nel log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
