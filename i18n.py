"""Idioma por trabajo (es/en). Se fija en run_job y lo heredan las tareas y los hilos (asyncio.to_thread)."""
import contextvars
import os

LANG = contextvars.ContextVar("vialert_lang", default="es")


ALLOW_EN = os.getenv("VIALERT_ALLOW_EN", "0") == "1"   # por defecto TODO es espanol; el ingles queda apagado


def en() -> bool:
    return ALLOW_EN and LANG.get() == "en"


def pick(es: str, en_: str) -> str:
    return en_ if en() else es
