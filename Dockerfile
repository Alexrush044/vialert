# Vialert: analisis de incidentes con YOLO (local) + VLM (Huawei Cloud MaaS)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-yolo.txt ./
# PyTorch CPU + Ultralytics (YOLO). YOLO=0 en .env lo desactiva en tiempo de ejecucion.
RUN pip install -r requirements.txt \
    && pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements-yolo.txt \
    && pip uninstall -y opencv-python >/dev/null 2>&1; pip install --force-reinstall --no-deps opencv-python-headless
COPY . .
# Los pesos de YOLO viajan en el repo (models/): no dependen de que la ECS alcance GitHub. Si no cargan, el build FALLA aqui.
RUN python -c "from ultralytics import YOLO; import glob; [YOLO(p) for p in sorted(glob.glob('models/*.pt'))]; print('YOLO OK')"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
