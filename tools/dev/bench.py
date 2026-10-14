"""Benchmark de latencia desde el ECS: python bench.py imagen.jpg [N]
Requiere MAAS_API_KEY. Mide P50/P95 con thinking desactivado."""
import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
import asyncio, base64, os, sys, time, statistics as st
import httpx
from app import MAAS_URL, MAAS_MODEL, SYSTEM_PROMPT, MAX_TOKENS
async def main(path, n):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    body = {"model": MAAS_MODEL, "thinking": {"type": "disabled"}, "max_tokens": MAX_TOKENS, "temperature": 0.1,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                                                      {"type": "text", "text": "Clasifica el ultimo frame."}]}]}
    h = {"Authorization": "Bearer " + os.environ["MAAS_API_KEY"]}
    ts = []
    async with httpx.AsyncClient(timeout=30) as c:
        for i in range(n):
            t = time.perf_counter(); r = await c.post(MAAS_URL, json=body, headers=h)
            ts.append(time.perf_counter() - t); print(i, r.status_code, f"{ts[-1]:.2f}s", r.text[:120].replace("\n", " "))
    ts2 = sorted(ts); print(f"P50={st.median(ts):.2f}s  P95={ts2[min(len(ts2)-1, int(0.95*len(ts2)))]:.2f}s  max={max(ts):.2f}s")
asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 10))
