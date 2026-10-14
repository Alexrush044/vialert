# -*- coding: utf-8 -*-
"""FunctionGraph (Event Function, Python 3.x) - dispara Vialert cuando llega un video nuevo a OBS.

Variables de entorno de la funcion (Configuration > Environment Variables):
  INGEST_URL    http://IP_O_DNS_DE_VIALERT:8000/api/ingest   (o la IP privada si la funcion esta en la VPC)
  INGEST_TOKEN  el mismo valor que INGEST_TOKEN en el .env de Vialert
Handler: index.handler        Solo usa la libreria estandar (sin dependencias).
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import unquote_plus

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".ts", ".avi", ".m4v", ".flv")


def _cfg(context, name, default=""):
    try:
        v = context.getUserData(name)
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get(name, default)


def _records(event):
    """Extrae (bucket, key, event_name) del evento OBS (formato tipo S3)."""
    if isinstance(event, str):
        event = json.loads(event)
    out = []
    for r in (event or {}).get("Records", []):
        s3 = r.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        key = s3.get("object", {}).get("key")
        if bucket and key:
            out.append((bucket, unquote_plus(key), r.get("eventName", ""), s3.get("object", {}).get("eTag", "")))
    return out


def _post(url, token, payload, timeout=8):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Vialert-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()[:300]


def handler(event, context):
    url, token = _cfg(context, "INGEST_URL"), _cfg(context, "INGEST_TOKEN")
    if not url or not token:
        raise RuntimeError("Faltan INGEST_URL / INGEST_TOKEN en las variables de entorno de la funcion")
    sent = []
    for bucket, key, name, etag in _records(event):
        if not key.lower().endswith(VIDEO_EXT):
            print("ignorado (no es video):", key)
            continue
        try:
            status, body = _post(url, token, {"bucket": bucket, "key": key, "event_id": name + ":" + etag, "source": "functiongraph"})
            print("Vialert", status, body, "<-", bucket, key)
            sent.append({"key": key, "status": status})
        except urllib.error.HTTPError as e:
            print("Vialert HTTP", e.code, e.read().decode()[:200])
            raise
        except Exception as e:  # noqa: BLE001  (si falla, FunctionGraph reintenta el evento)
            print("No se pudo llegar a Vialert:", repr(e))
            raise
    return {"statusCode": 200, "body": json.dumps({"sent": sent})}
