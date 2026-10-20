#!/usr/bin/env python3
"""Riassume data/probe_log.jsonl: affidabilità e ritardo di ogni fonte."""
import json
import statistics
from pathlib import Path

LOG = Path(__file__).resolve().parent / "data" / "probe_log.jsonl"


def pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def describe(name, ok, total, lags):
    rate = f"{100 * ok / total:.0f}%" if total else "n/d"
    if lags:
        print(f"{name:<28} riuscite {ok}/{total} ({rate})  ritardo min: "
              f"mediana {statistics.median(lags):.0f}, 90° perc. {pct(lags, 0.9):.0f}, "
              f"max {max(lags):.0f}")
    else:
        print(f"{name:<28} riuscite {ok}/{total} ({rate})  ritardo: n/d")


def main():
    runs = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
    if not runs:
        print("Nessuna esecuzione registrata.")
        return
    print(f"{len(runs)} esecuzioni, dal {runs[0]['run_utc']} al {runs[-1]['run_utc']}\n")

    # Intervalli tra esecuzioni: quanto è puntuale GitHub Actions
    from datetime import datetime
    times = [datetime.fromisoformat(r["run_utc"]) for r in runs]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    if gaps:
        print(f"Intervallo tra esecuzioni (min): mediana {statistics.median(gaps):.0f}, "
              f"90° perc. {pct(gaps, 0.9):.0f}, max {max(gaps):.0f}\n")

    arpa = [r.get("arpa", {}) for r in runs]
    ok = sum(1 for a in arpa if a.get("request", {}).get("ok"))
    describe("ARPA livelli (mediana staz.)", ok, len(arpa),
             [a["lag_min_livelli_median"] for a in arpa if "lag_min_livelli_median" in a])
    describe("ARPA tutti i sensori (min)", ok, len(arpa),
             [a["lag_min"]["min"] for a in arpa if "lag_min" in a])

    for p in ("SRI", "CUM3"):
        items = [r.get("radar", {}).get(p, {}) for r in runs]
        describe(f"Radar {p}", sum(1 for i in items if "lag_min" in i), len(items),
                 [i["lag_min"] for i in items if "lag_min" in i])

    dls = [r["radar"]["download_test"] for r in runs if "download_test" in r.get("radar", {})]
    describe("Radar download GeoTIFF", sum(1 for d in dls if d.get("file", {}).get("ok")),
             len(dls), [])

    oms = [r["openmeteo"] for r in runs if "openmeteo" in r]
    describe("Open-Meteo", sum(1 for o in oms if o.get("request", {}).get("ok")), len(oms), [])
    fallback = sum(1 for o in oms if "fallback" in str(o.get("model")))
    if fallback:
        print(f"  (in {fallback} casi il modello ICON-2I non era disponibile: usato fallback)")

    # Errori più frequenti
    errors = {}
    for r in runs:
        for src in ("discovery", "arpa", "radar", "openmeteo"):
            blob = json.dumps(r.get(src, {}))
            for key in ("HTTP 4", "HTTP 5", "URLError", "timeout", "crash", "Timeout"):
                if key in blob:
                    errors[(src, key)] = errors.get((src, key), 0) + 1
    if errors:
        print("\nErrori registrati:")
        for (src, key), n in sorted(errors.items(), key=lambda x: -x[1]):
            print(f"  {src:<10} {key:<10} {n} volte")

    # Sensori che non pubblicano mai
    last = next((r["arpa"] for r in reversed(runs) if "lag_min_per_sensor" in r.get("arpa", {})), None)
    if last:
        stale = {k: v for k, v in last["lag_min_per_sensor"].items() if v > 180}
        if stale:
            print(f"\nSensori con dato più vecchio di 3 ore nell'ultima esecuzione: {len(stale)}")


if __name__ == "__main__":
    main()
