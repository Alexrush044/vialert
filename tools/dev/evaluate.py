"""Evaluacion por lote de los casos de prueba (casos/casos.json + videos).

Uso:  python evaluate.py                # todos los casos disponibles
      python evaluate.py colision congestion   # solo algunos
Genera informe_<fecha>.json y .csv con veredicto por caso y precision/recall por clase.
Requiere MAAS_API_KEY (sin ella corre en modo simulado y los numeros NO son reales).
"""
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
import asyncio
import csv
import json
import sys
import time

from app import (CASES_DIR, LABELS, MAAS_MODEL, MOCK, NORMAL, evaluate_case, latency_stats, load_cases,
                 pipeline, prepare_video, FRAME_INTERVAL, CONTEXT_FRAMES)


async def main(only: list[str]):
    if MOCK:
        print("!! MODO SIMULADO: no hay MAAS_API_KEY. Los resultados no son reales.\n")
    cases = [c for c in load_cases() if c["available"] and (not only or c["id"] in only)]
    if not cases:
        print("No hay casos con video en casos/. Copia los videos con los nombres de casos.json.")
        return
    tally = {l: {"tp": 0, "fn": 0, "fp": 0} for l in LABELS if l != NORMAL}
    rows, lat = [], []
    for c in cases:
        path = await asyncio.to_thread(prepare_video, CASES_DIR / c["file"])

        async def emit(_):
            pass

        results = await pipeline(path, emit, speed=1000)
        v = evaluate_case(c["expected"], c.get("person_down", False), results)
        # latencia sin el tiempo de espera en cola (aqui se lanzan todos los frames juntos)
        lat += [r["total_ms"] - r["queue_ms"] for r in results if not r.get("error")]
        exp, det = set(v["expected"]), set(v["detected"])
        for l in exp & det:
            tally[l]["tp"] += 1
        for l in exp - det:
            tally[l]["fn"] += 1
        for l in det - exp:
            tally[l]["fp"] += 1
        rows.append({"caso": c["id"], "estado": v["status"], "esperado": "+".join(v["expected"]) or NORMAL,
                     "detectado": "+".join(v["detected"]) or NORMAL, "faltantes": "+".join(v["missed"]),
                     "extras": "+".join(v["extra"]), "frames_ok": v["frames_ok"], "frames_error": v["frames_error"]})
        print(f"[{v['status'].upper():7}] {c['id']:20} esperado={rows[-1]['esperado']}  detectado={rows[-1]['detectado']}"
              + (f"  errores={v['frames_error']}" if v["frames_error"] else ""))

    print("\nClase                      TP  FN  FP  precision  recall")
    for l, t in tally.items():
        if t["tp"] + t["fn"] + t["fp"] == 0:
            continue
        p = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else float("nan")
        r = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else float("nan")
        print(f"{l:26} {t['tp']:3} {t['fn']:3} {t['fp']:3}  {p:9.2f}  {r:6.2f}")
    passed = sum(r["estado"] == "pass" for r in rows)
    print(f"\nCasos: {passed}/{len(rows)} aprobados, {sum(r['estado']=='partial' for r in rows)} parciales.")
    st = latency_stats(lat)
    print(f"Latencia por llamada: P50={st['p50']} ms  P95={st['p95']} ms  max={st['max']} ms  (<=7 s: {st['in_budget_pct']}%)")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    meta = {"model": MAAS_MODEL, "mock": MOCK, "interval_s": FRAME_INTERVAL, "context_frames": CONTEXT_FRAMES}
    with open(f"informe_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump({**meta, "casos": rows, "por_clase": tally, "latencia": st}, f, ensure_ascii=False, indent=2)
    with open(f"informe_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Guardado: informe_{stamp}.json / .csv")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
