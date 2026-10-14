# FunctionGraph: aviso de video nuevo en OBS

Esta función **solo avisa**: cuando llega un video al bucket, hace `POST /api/ingest` a Vialert (con el token compartido). Vialert descarga el video desde OBS, lo analiza (YOLO + VLM) y escribe `report.json` y `evidence_*.jpg` de vuelta en el mismo bucket bajo `OBS_OUT_PREFIX` (por defecto `vialert-results/`).

## Crear la función (Huawei Cloud → FunctionGraph)
| Campo | Valor |
|---|---|
| Tipo | **Función de eventos** (Event Function) |
| Runtime | Python 3.9 o superior (probada con 3.12) |
| Código | pega `index.py` (solo usa la biblioteca estándar, sin dependencias) |
| Handler | `index.handler` |
| Timeout | 30 s · Memoria 128 MB |
| Acceso a red | pública (si Vialert tiene IP pública) o dentro de la VPC (si Vialert está en ella) |

**Variables de entorno de la función**
- `INGEST_URL` = `http://IP_O_DNS_DE_VIALERT:8000/api/ingest`
- `INGEST_TOKEN` = el mismo valor que `INGEST_TOKEN` en el `.env` de Vialert (`openssl rand -hex 16` para generarlo)

## Disparador (Trigger) OBS
- Tipo **OBS**, bucket de los videos, eventos de creación de objetos (Put, Post, Copy, Complete Multipart Upload).
- **Prefijo** de la carpeta de entrada (por ejemplo `incoming/`, **con la diagonal final**) y **sufijo** `.mp4`.
- La carpeta de resultados (`vialert-results/`) debe quedar **fuera** del prefijo de entrada. Vialert además ignora lo que está bajo `OBS_OUT_PREFIX` para evitar bucles.

## Lado Vialert (`.env`)
`INGEST_TOKEN`, `OBS_ENDPOINT` (el de la región del bucket), `OBS_BUCKETS`, y `OBS_AK`/`OBS_SK` (o `OBS_USE_AGENCY=1` con una agencia IAM asignada al servidor). El puerto de Vialert (8000) debe ser alcanzable desde la función: la función sale de IP variables, así que o se abre el puerto (protegido por el token) o se pone la función en la misma VPC.

## Probar sin subir un video
En la consola de FunctionGraph → **Probar** con este evento (cambia bucket y key por uno que exista):
```json
{"Records":[{"eventName":"ObjectCreated:Put","s3":{"bucket":{"name":"MI_BUCKET"},"object":{"key":"incoming/prueba.mp4","eTag":"abc"}}}]}
```
Resultado esperado en el log: `Vialert 202 …`. En Vialert, pestaña **Bucket OBS**, el video aparece y avanza por sus etapas.
Equivalente desde cualquier terminal (sin FunctionGraph):
```bash
curl -X POST http://SERVIDOR:8000/api/ingest -H "X-Vialert-Token: TOKEN" -H "content-type: application/json" \
     -d '{"bucket":"MI_BUCKET","key":"incoming/prueba.mp4"}'
```

## Errores frecuentes
| En el log de la función | Causa |
|---|---|
| `HTTP Error 401` | `INGEST_TOKEN` distinto al del `.env` de Vialert |
| `HTTP Error 503` | Vialert sin `INGEST_TOKEN` o sin OBS configurado |
| `timed out` / `Connection refused` | `INGEST_URL` mal, puerto 8000 cerrado o Vialert apagado |
| No se ejecuta nunca | prefijo/sufijo del disparador (`incoming/` con diagonal, `.mp4` en minúsculas) |
