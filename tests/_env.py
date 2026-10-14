"""Entorno comun de las pruebas: se importa ANTES que `app` (app lee el entorno al importarse)."""
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="vialert_test_")
OBS = Path(tempfile.mkdtemp(prefix="vialert_obs_"))   # carpeta distinta: EVENTS_DIR lista sus subcarpetas como camaras
os.environ.update({"EVENTS_DIR": TMP, "CAM_MAX": "3", "MTX_PUBLISH_PASS": "Clave123", "MTX_PUBLISH_USER": "cam",
                   "MAAS_API_KEY": "", "DEMO_USER": "", "DEMO_PASS": "", "WEBHOOK_URL": "", "YOLO": "0", "FIRE": "1",
                   "OBS_LOCAL_DIR": str(OBS), "INGEST_TOKEN": "tok-123", "INGEST_RETRIES": "0", "FRAME_INTERVAL": "3"})
