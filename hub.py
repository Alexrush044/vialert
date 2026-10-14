"""Cola de eventos con varios suscriptores y repeticion del historial reciente.
Sirve para camaras que corren 24/7: la interfaz puede abrir y cerrar la vista de una camara en cualquier momento
y recibe el estado actual (inicio, ultimos frames y resultados, ultima analitica) antes de seguir en vivo."""
import asyncio
import collections


class Hub:
    KEEP = 300                    # capturas/resultados recientes que se repiten al conectarse

    def __init__(self):
        self.start = None
        self.last: dict[str, dict] = {}
        self.hist: collections.deque = collections.deque(maxlen=self.KEEP)
        self.subs: set[asyncio.Queue] = set()
        self.done = None

    async def put(self, ev: dict):
        t = ev["type"]
        if t == "start":
            self.start = ev
        elif t in ("analytics", "incidents", "live"):
            self.last[t] = ev
        elif t == "done":
            self.done = ev
        else:
            self.hist.append(ev)
        for q in list(self.subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:          # cliente demasiado lento: se le corta
                self.subs.discard(q)

    def subscribe(self):
        """Devuelve (eventos_actuales, cola). Sin awaits entre ambos: no se pierde ni se duplica nada."""
        snap = ([self.start] if self.start else []) + list(self.hist) + \
               [self.last[k] for k in ("analytics", "incidents", "live") if k in self.last] + ([self.done] if self.done else [])
        q: asyncio.Queue = asyncio.Queue(maxsize=3000)
        if not self.done:
            self.subs.add(q)
        return snap, q

    def unsubscribe(self, q):
        self.subs.discard(q)
