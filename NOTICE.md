# Licencias de terceros

| Componente | Uso | Licencia |
|---|---|---|
| [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) (`models/yolo11n.pt` y `models/yolo11s.pt`, incluidos en el repo) | detección local de objetos | **AGPL-3.0** (uso comercial/cerrado: licencia empresarial de Ultralytics) |
| [MediaMTX](https://github.com/bluenviron/mediamtx) | pasarela RTSP/RTMP (imagen oficial `bluenviron/mediamtx`) | MIT |
| Huawei Cloud OBS Python SDK (`obs/`, vendorizado) | acceso a OBS | Apache-2.0 (ver `obs/LICENSE`) |
| FastAPI, Uvicorn, httpx, OpenCV, NumPy, PyTorch, segno | núcleo | MIT / BSD / Apache-2.0 |
| Albert Sans, Red Hat Mono (`static/fonts`) | tipografías | SIL OFL 1.1 |

Antes de distribuir Vialert a terceros revisa las condiciones de **Ultralytics (AGPL-3.0)**: si no puedes cumplirlas, compra la licencia empresarial o sustituye el detector.
