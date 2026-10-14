"""Acceso a OBS (Huawei Cloud) para el Escenario 2. Usa el SDK oficial (esdk-obs-python), cargado solo si hace falta.

Variables (.env):  OBS_ENDPOINT (p. ej. https://obs.la-north-2.myhuaweicloud.com), OBS_AK, OBS_SK,
OBS_USE_AGENCY=1 (alternativa a AK/SK: agencia de IAM asignada a la ECS), OBS_BUCKETS (opcional, lista permitida), OBS_OUT_PREFIX (donde se escriben los resultados).
OBS_LOCAL_DIR: solo para pruebas sin OBS: trata una carpeta local como si fuera el bucket (<dir>/<bucket>/<key>).
"""
import os
import shutil
from pathlib import Path

ENDPOINT = os.getenv("OBS_ENDPOINT", "")
AK = os.getenv("OBS_AK", "")
SK = os.getenv("OBS_SK", "")
LOCAL_DIR = os.getenv("OBS_LOCAL_DIR", "")
USE_AGENCY = os.getenv("OBS_USE_AGENCY", "0") == "1"   # credenciales temporales de la agencia asignada a la ECS (sin AK/SK)
OUT_PREFIX = os.getenv("OBS_OUT_PREFIX", "vialert-results/").lstrip("/")
ALLOWED = [b.strip() for b in os.getenv("OBS_BUCKETS", "").split(",") if b.strip()]
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".ts", ".avi", ".m4v", ".flv")

_client = None


def configured() -> bool:
    return bool(LOCAL_DIR or (ENDPOINT and ((AK and SK) or USE_AGENCY)))


def bucket_ok(bucket: str) -> bool:
    return not ALLOWED or bucket in ALLOWED


def _cli():
    global _client
    if _client is None:
        from obs import ObsClient  # esdk-obs-python
        if AK and SK:
            _client = ObsClient(access_key_id=AK, secret_access_key=SK, server=ENDPOINT)
        else:  # el SDK busca credenciales en ENV -> metadatos de la ECS (agencia) -> OIDC
            _client = ObsClient(server=ENDPOINT)
    return _client


def _check(r, what: str):
    if getattr(r, "status", 500) >= 300:
        raise RuntimeError(f"OBS {what}: {getattr(r, 'status', '?')} {getattr(r, 'errorCode', '')} {getattr(r, 'errorMessage', '')}".strip())
    return r


def download(bucket: str, key: str, dest: Path) -> int:
    if LOCAL_DIR:
        src = Path(LOCAL_DIR) / bucket / key
        shutil.copyfile(src, dest)
        return dest.stat().st_size
    _check(_cli().getObject(bucket, key, downloadPath=str(dest)), f"getObject {bucket}/{key}")
    return dest.stat().st_size


def upload_bytes(bucket: str, key: str, data: bytes, content_type: str = "application/json"):
    if LOCAL_DIR:
        p = Path(LOCAL_DIR) / bucket / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return
    _check(_cli().putContent(bucket, key, content=data, metadata=None, headers=None), f"putObject {bucket}/{key}")


def upload_file(bucket: str, key: str, path: Path):
    if LOCAL_DIR:
        p = Path(LOCAL_DIR) / bucket / key
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)
        return
    _check(_cli().putFile(bucket, key, str(path)), f"putFile {bucket}/{key}")
