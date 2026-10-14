#!/usr/bin/env bash
# Uso en el ECS (Ubuntu 24.04):  cp .env.example .env ; nano .env ; ./run.sh
# La primera vez instala ffmpeg, PyTorch (CPU) y Ultralytics (YOLO): tarda unos minutos.
# YOLO=0 en .env (o en el entorno) lo omite por completo.
set -e
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
if ! command -v ffmpeg >/dev/null || ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Instalando paquetes del sistema..."
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq ffmpeg ca-certificates python3-venv python3-pip libglib2.0-0 libgl1
fi
python3 -m venv .venv 2>/dev/null || true
. .venv/bin/activate
pip install -q -r requirements.txt
if [ "${YOLO:-1}" = "1" ] && [ ! -f .venv/.yolo_ready ]; then
  echo "Instalando YOLO (PyTorch CPU + Ultralytics)... primera vez, unos minutos"
  if ! python -c "import torch, torchvision" 2>/dev/null; then
    pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu \
      || pip install -q torch torchvision || echo ">> No se pudo instalar PyTorch: se continua sin YOLO"
  fi
  if python -c "import torch" 2>/dev/null; then
    pip install -q -r requirements-yolo.txt || echo ">> No se pudo instalar ultralytics: se continua sin YOLO"
    # ultralytics puede traer opencv-python (con GUI); dejamos solo la version headless
    pip uninstall -y -q opencv-python >/dev/null 2>&1 || true
    pip install -q --force-reinstall --no-deps opencv-python-headless >/dev/null 2>&1 || true
    if python -c "import ultralytics" 2>/dev/null; then
      python - <<'PY' && touch .venv/.yolo_ready || echo ">> No se pudieron descargar los pesos de YOLO (GitHub). Ver README."
import os
from ultralytics import YOLO
YOLO(os.getenv("YOLO_WEIGHTS", "yolo11n.pt"))
print("pesos YOLO listos")
PY
    fi
  fi
fi
[ -z "$MAAS_API_KEY" ] && echo ">> Sin MAAS_API_KEY: modo SIMULADO (resultados no reales)"
python -c "import ultralytics" 2>/dev/null && echo ">> YOLO disponible" || echo ">> YOLO no instalado: solo muestreo por movimiento"
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
