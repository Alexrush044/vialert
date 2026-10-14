# Vialert

Detección de incidentes (viales y de seguridad ciudadana) en video **en tiempo casi real**, con varias cámaras a la vez.
YOLO corre en tu servidor (barato, local) y selecciona la evidencia; un modelo de visión (VLM, Huawei Cloud MaaS) la interpreta.
Cada cámara guarda su bitácora por día y se consulta desde la propia interfaz.

*English summary: self-hosted incident detection for live cameras. Phones/encoders stream to the server (RTSP/RTMP) or the server pulls IP cameras (RTSP); YOLO picks the evidence, a cloud VLM analyzes it, and every camera keeps a daily incident log. Install with `docker compose up -d --build`, then add cameras from the web UI.*

```
 celular / encoder ──RTSP/RTMP──▶ MediaMTX ──┐
                                             ├─▶ Vialert ─▶ YOLO (local) ─▶ evidencia ─▶ VLM (MaaS) ─▶ incidente
 cámara IP / NVR ◀────── RTSP (Vialert se conecta) ┘                                              │
                                                              bitácora por cámara y por día ◀─────┤
 video en OBS ─▶ FunctionGraph ─▶ /api/ingest (Escenario 2)                      webhook ◀────────┘
```

## Requisitos
- Servidor Linux con **4 vCPU / 8 GiB** como mínimo (YOLO en CPU; cada cámara activa suma carga), salida a internet hacia MaaS.
- **Docker + Docker Compose** (recomendado) o Ubuntu 22.04/24.04 con Python 3.12.
- Una **API key de Huawei Cloud MaaS** (`MAAS_API_KEY`). Sin ella, Vialert corre en modo SIMULADO (resultados no reales).
- Puertos: `8000` (interfaz). `8554` (RTSP) y `1935` (RTMP) **solo** si habrá celulares/equipos que transmiten al servidor (con cámaras IP no se necesitan).

## Instalación con Docker (recomendada)
```bash
git clone https://github.com/Alexrush044/vialert.git vialert && cd vialert
cp .env.example .env
nano .env        # MAAS_API_KEY, DEMO_USER / DEMO_PASS
docker compose up -d --build                      # cámaras IP / NVR
# docker compose --profile publish up -d --build  # además celulares/equipos que transmiten (define MTX_PUBLISH_PASS y MTX_READ_PASS)
python3 tools/selfcheck.py http://localhost:8000 admin tu_clave     # revisa que todo esté en orden
```
Abre `http://SERVIDOR:8000`. La primera construcción tarda varios minutos (PyTorch CPU + YOLO). Los datos (`data/events`, `data/logs`) persisten en el servidor.
Actualizar: `git pull && docker compose up -d --build`.

## Instalación sin Docker (Ubuntu)
```bash
cp .env.example .env && nano .env
# sudo bash tools/install_mediamtx.sh      # OPCIONAL: solo si habrá celulares/equipos que transmiten (imprime la clave de publicación)
./run.sh                                    # crea .venv, instala dependencias y arranca en :8000
```
Como servicio: copia `tools/vialert.service` a `/etc/systemd/system/`, ajusta la ruta y `systemctl enable --now vialert`.

## Agregar cámaras (todo desde la interfaz)
Pestaña **Cámaras → + Agregar cámara**. No necesitas tocar la terminal. Puedes registrar las que quieras (`CAM_MAX`, 8 por defecto); cada una se analiza por separado.

**Cámara IP / NVR (RTSP): el caso normal en producción.** Vialert se conecta a la cámara; **no** necesita MediaMTX ni abrir puertos hacia internet.
1. Elige la marca (rellena la ruta típica), escribe IP y puerto, y usuario y contraseña **de la cámara** (déjalos vacíos si la cámara no pide credenciales).
2. Pulsa **Probar conexión**. El diagnóstico dice qué falla: clave incorrecta, puerto cerrado, ruta inexistente (404), sin ruta de red, sin video H.264/H.265.
3. Guarda. Usa el *sub-stream* si existe: pesa menos y basta para analizar.
El servidor debe llegar a la cámara (misma red, VPN o IP pública con el puerto 554 reenviado).

**Celular o equipo que transmite** (opcional, para pruebas o equipos móviles; necesita MediaMTX: `docker compose --profile publish up -d` o `tools/install_mediamtx.sh`, y abrir 8554/1935):
1. Elige *Celular o equipo que transmite* y ponle nombre.
2. Vialert muestra la URL, el usuario, la clave y un **QR** para la app *Larix Broadcaster* (Android/iOS). Video recomendado: H.264, 1280×720, 15–25 fps, ~2 Mbps, sin audio.
3. Al empezar a transmitir, el asistente detecta la señal y (si lo dejas marcado) **inicia el análisis solo**. También sirve con drones o codificadores que publiquen por RTSP/RTMP.
La clave de publicación (`MTX_PUBLISH_PASS`) es solo de este camino; las cámaras IP no la usan.

Cada tarjeta permite Iniciar/Detener, Probar conexión, Editar y Eliminar (conservando o borrando su historial). Las cámaras se guardan en `events/cameras.json` (permisos 600; contiene las claves de las cámaras IP: protégelo como un secreto).

## Consulta de incidentes
- **Registros** (botón arriba a la derecha): cada incidente con **fuente** (cámara, o `bucket/archivo.mp4` si vino de OBS), **fecha, hora** y segundo dentro del video; todas las fuentes o las que elijas, rango de fechas (Hoy / Ayer / 7 días / Todo), tipo de evento, severidad, "solo persona caída" y búsqueda por texto; resumen por cámara y por tipo, matriz día × cámara, lista con evidencia y **exportar CSV**.
- **Historial por cámara** (al pie de la pestaña Cámaras): un día, estadísticas, incidentes con evidencia, CSV/JSONL.
- Bitácora en disco: `events/<cámara>/<AAAA-MM-DD>/` → `events.jsonl`, `frames.jsonl`, `analytics.jsonl`, `img/`. Los incidentes iguales a menos de `EVENT_MERGE_S` s se agrupan. Retención `EVENTS_KEEP_DAYS` (30).
- API: `GET /api/logs`, `GET /api/logs/csv` (`cams, frm, to, ev, sev, down, q, limit`).

## Webhook
Define `WEBHOOK_URL` en `.env` y cada incidente se envía como `POST` JSON:
```json
{"type":"incident","source":"Entrada principal","event":"COLLISION","severity":"high","confidence":0.93,
 "person_down":false,"flags":[],"reason":"…","scene":"…","ts":"2026-10-13T12:30:00-0600"}
```
Opciones: `WEBHOOK_TOKEN` (cabeceras `Authorization: Bearer` y `X-Vialert-Token`), `WEBHOOK_MIN_SEV`, `WEBHOOK_IMAGE=1` (frame en base64), `WEBHOOK_COOLDOWN_S`, `WEBHOOK_ALERTS=1`.
Prueba: `curl -X POST -u admin:clave http://localhost:8000/api/webhook/test`.

## Escenario 2: video en OBS → FunctionGraph → Vialert
Un video que cae en un bucket dispara una función que avisa a Vialert; este lo descarga, analiza y escribe el informe y la evidencia de vuelta en el bucket. El **código de la función y su guía de creación** (tipo, handler, variables, disparador, evento de prueba, errores) están en [`functiongraph/`](functiongraph/README.md); el resumen del flujo, en [`docs/ESCENARIO2_OBS.md`](docs/ESCENARIO2_OBS.md).

## Costo y capacidad (estimaciones, mídelas con tus cámaras)
- Con 4 cámaras activas se estiman ~80 mil tokens/min y 50–80 peticiones/min hacia MaaS. `RPM_LIMIT` (90) protege la cuota; `CAM_MAX_MIN` corta cada sesión automáticamente (0 = sin corte).
- YOLO comparte un candado de inferencia. El perfil por defecto es **detallado** (`yolo11s`, 800 px, 3 muestras/s en archivos y OBS, 2 en vivo): con 4 vCPU conviene ≤ 3 cámaras en vivo; con más, usa `YOLO_WEIGHTS=yolo11n.pt YOLO_IMGSZ=640` o sube vCPU. Los pesos vienen en `models/` (no se descargan de internet).

## Seguridad (léelo antes de exponer el servidor)
- Define `DEMO_USER` y `DEMO_PASS`. Basic Auth **no cifra** el tráfico por HTTP: pon HTTPS delante (ejemplo con Caddy: `vialert.tu-dominio.com { reverse_proxy localhost:8000 }`) o limita el puerto por IP.
- Si usas celulares/equipos que transmiten: abre `8554`/`1935` solo a quien transmite; la clave `MTX_PUBLISH_PASS` protege esos puertos (usa una larga y única). Con cámaras IP no abras esos puertos.
- La API key y las claves de cámaras nunca se muestran en la interfaz ni en logs; la clave de publicación sí se muestra a quien entra con usuario y clave (para configurar celulares).
- `/api/ingest` (Escenario 2) usa su propio token (`INGEST_TOKEN`) y queda fuera de Basic Auth.
- Una cámara IP permite que el servidor se conecte a la URL que le indiques: por eso la interfaz debe estar protegida. `LIVE_ALLOWED_HOSTS` limita a qué hosts.

## Solución de problemas
| Síntoma | Qué revisar |
|---|---|
| "Esperando señal del celular" no cambia (solo camino celular) | ¿Larix está transmitiendo? ¿Puertos 8554/1935 abiertos? ¿clave correcta? `docker compose logs mediamtx` |
| Prueba: *usuario o contraseña incorrectos* | Credenciales de la cámara (o de lectura de MediaMTX: `MTX_READ_PASS`) |
| Prueba: *ruta no existe (404)* | Ruta del stream de la cámara (manual: «RTSP URL») |
| Prueba: *puerto cerrado* | Puerto RTSP de la cámara (554 por defecto) / RTSP desactivado en la cámara |
| Prueba: *sin ruta / tiempo agotado* | IP, VPN, firewall, que la cámara esté encendida |
| La interfaz dice SIMULADO | Falta `MAAS_API_KEY` en `.env` (reinicia después) |
| `.env` cambiado y nada cambia | Reinicia: `docker compose up -d` / reinicia `./run.sh` |
| `python3 tools/selfcheck.py` | Lista qué falta y cómo resolverlo |

## Pruebas
`python -m unittest discover -s tests -v` (humo: cámaras, claves, límites, consulta, interfaz; no requiere red ni MaaS).

## Más documentación
- [`docs/ANALISIS.md`](docs/ANALISIS.md): cómo analiza (muestreo híbrido, YOLO, eventos, zonas, logs, ajustes).
- [`docs/ESCENARIO2_OBS.md`](docs/ESCENARIO2_OBS.md): video → OBS → FunctionGraph → Vialert.
- [`.env.example`](.env.example): todas las variables comentadas. [`NOTICE.md`](NOTICE.md): licencias de terceros. [`CHANGELOG.md`](CHANGELOG.md).
