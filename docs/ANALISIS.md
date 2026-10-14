# Cómo analiza Vialert (referencia técnica)

## Cómo analiza el video
- **Muestreo híbrido:** un frame base cada `FRAME_INTERVAL` s (5) + frames extra justo después de cada pico de movimiento (pre-escaneo local barato). Así no se pierde un evento que ocurre entre dos frames base.
- **Contexto:** cada solicitud lleva también el frame de `CONTEXT_GAP_S` (1.5) s antes, para distinguir movimiento de objetos detenidos.
- **YOLO (Ultralytics, `yolo11s` por defecto)** corre en el ECS antes del VLM: detecta autos/motos/camiones/personas/objetos/animales/cuchillos y bates (3 muestras/s), los sigue y levanta alertas (vehículo que se detiene tras circular, frenado brusco —también junto a otro vehículo—, persona en posición horizontal). Cada alerta añade un frame extra para el modelo y, con `YOLO_HINTS=1`, un resumen corto de texto como pista. **YOLO no clasifica el incidente**: lo decide siempre el VLM. Las cajas se ven sobre el video (casilla "Ver cajas").
- Recorte de bordes negros: opcional, apagado por defecto (`AUTOCROP=1` para capturas de celular).
- La tira "Frames analizados" muestra exactamente lo que recibe el modelo.

## Eventos entre personas (seguridad ciudadana)
Además de los 10 viales, el modelo puede etiquetar `ASSAULT_OR_ROBBERY` (asalto/robo), `FIGHT_OR_VIOLENCE` (pelea/golpes), `PROTEST_OR_BLOCKADE` (manifestación/paro/bloqueo) y `CROWD_GATHERING` (aglomeración/evento social). YOLO ayuda con dos alertas: `aglomeracion` (≥ `YOLO_CROWD_N`=6 personas a la vez) y `forcejeo` (dos personas que se solapan y se mueven). El prompt exige evidencia visible y pide describir solo conductas, sin identificar personas. Casos de prueba 13–16 en `casos/casos.json` (el 16 es control negativo).
Limitación: con un frame cada 5 s una pelea o un asalto rápido puede caer entre dos frames; los frames extra de YOLO/movimiento lo mitigan, no lo eliminan.

## Más eventos (v6)
**Del modelo (VLM):** `VANDALISM_OR_DAMAGE`, `WRONG_WAY_VEHICLE` (contraflujo), `PEDESTRIAN_STRUCK` (atropellamiento, en lugar de COLLISION), `ILLEGAL_PARKING` (doble fila/zona prohibida), `ABANDONED_OBJECT`, `CAMERA_OBSTRUCTED`, `LOW_VISIBILITY_OR_FLOOD` (niebla/lluvia/inundación) y `RISK_BEHAVIOR`. Además el JSON trae `flags`: `armed_person` (persona armada), `no_helmet`, `moto_overload` (moto con 3+). Una bandera sin otro evento se registra como `RISK_BEHAVIOR`.

**Analítica local (sin nube), panel "Analítica local" + carril en la línea de tiempo + `analytics.json` en los logs y en el informe:**
- Calidad de cámara (`quality.py`): `cam:tapada`, `cam:oscura`, `cam:desenfocada`, `cam:movida`, `cam:congelada`. Umbrales `Q_*` en `.env`; `QUALITY=0` la apaga.
- YOLO: `yolo:merodeo` (≥`YOLO_LOITER_S`=20 s en el mismo lugar), `yolo:objeto_abandonado` (mochila/bolso/maleta ≥`YOLO_ABANDON_S`=15 s sin persona cerca; propenso a falsos positivos), `yolo:intrusion` y `yolo:estacionado_prohibido` (requieren zonas).
- **Zonas:** botones "Zona restringida" y "No estacionar" para dibujar el polígono sobre el video (clic en vértices, cerrar sobre el primer punto o doble clic). En los casos se fijan en `casos.json` → `zones`.
- **Conteo y flujo (aproximado):** vehículos/personas únicos, por minuto, ocupación de la imagen, velocidad relativa, tendencia y estado (fluido/denso/congestionado). Es una estimación con un seguimiento simple, no un conteo por línea de cruce.
- Los eventos de analítica disparan un frame extra para el modelo y viajan como pista. Los casos pueden exigirlos (`"analytics": [...]`) y cuentan en el veredicto.
- **Doble fila:** no se puede saber sin la geometría de los carriles; la decide el modelo (`ILLEGAL_PARKING`), con poca fiabilidad.
- El prompt ahora es más largo (≈+400 tokens de entrada por frame).

## YOLO en el ECS
- `./run.sh` o la imagen Docker instalan PyTorch (CPU) + Ultralytics. **Los pesos (`yolo11n.pt`, `yolo11s.pt`) vienen en `models/`**: no se descargan de internet. Al arrancar Vialert carga el modelo y hace una inferencia de prueba; si falla, avisa en el log (`YOLO NO CARGO`), en un banner rojo de la interfaz y en `tools/selfcheck.py`. Sin YOLO la app sigue, pero con menos frames extra y sin alertas. `YOLO=0` lo desactiva a propósito.
- Perfil por defecto "detallado": `YOLO_WEIGHTS=yolo11s.pt`, `YOLO_IMGSZ=800`, `YOLO_CONF=0.25`, `YOLO_FPS=3` (`YOLO_LIVE_FPS=2` en vivo). Referencia medida en un servidor de pruebas (CPU, sin GPU): ≈0.2 s por imagen con `yolo11n`/640 y ≈0.4 s con `yolo11s`/800. Mídelo en tu ECS con `python tools/dev/yolo_check.py`.

## Fuego y humo
YOLO (COCO) **no tiene** clases de fuego ni humo. `fire.py` vigila cada muestra del pre-escaneo, sin modelo de IA:
- **fuego** (`fire:fuego`): píxeles naranja/amarillo muy brillantes y saturados que parpadean sin desplazarse (un cono o un camión naranja no disparan).
- **humo** (`fire:humo`): zona gris poco saturada que se mueve y crece de forma sostenida (≥ `SMOKE_MIN_S`=3 s).
Solo es un **disparador**: pide un frame extra al VLM, que decide si es `FIRE_OR_SMOKE`. Un falso positivo cuesta un frame. Ajustes: `FIRE=0` lo apaga; `FIRE_MIN_FRAC`, `FIRE_FLICK`, `FIRE_MIN_S`, `SMOKE_MIN_FRAC`. Validado con escenas sintéticas (llama, humo, cono, camión naranja, escena normal); pruébalo con tus videos reales de incendio.

## Reglas nuevas de YOLO
`yolo:contacto_vehiculos` (dos vehículos encimados y uno frena de golpe), `yolo:persona_vehiculo` (persona encimada con un auto/bus/camión, fuera de su centro; ignora motociclistas), `yolo:animal`, `yolo:objeto_peligroso` (cuchillo o bate junto a una persona; propenso a falsos positivos). Umbrales más sensibles por defecto (`YOLO_STOP_S`=3, `YOLO_LIE_RATIO`=1.1, `YOLO_CROWD_N`=5, `YOLO_CONTACT_IOU`=0.10, `YOLO_LOITER_S`=15).
- `python tools/dev/yolo_check.py` mide ms/imagen reales en el ECS.
- Licencia: Ultralytics es AGPL-3.0; para uso comercial/cerrado se requiere licencia empresarial.

## Logs y diagnóstico
- Panel **Registro del modelo** (abajo en la interfaz): por frame muestra tiempos (total / API / cola), tokens de entrada y salida, imágenes enviadas, la escena que describe el modelo, su respuesta cruda y los frames exactos.
- Botón **Explicar con razonamiento**: reenvía ese frame con el razonamiento (thinking) activado. Solo diagnóstico: tarda 10-40 s.
- En el servidor, cada análisis deja `logs/<job>/` con `log.jsonl`, `prompt.txt`, `config.json`, `report.json` y los JPEG enviados; además `logs/vialert.log` (rotativo). Se borran a las 24 h.
- `DESCRIBE=0` quita la descripción de escena (menos latencia y menos tokens de salida).

## Ajustes (.env)
`CONTEXT_FRAMES=0` (solo frame actual, más rápido), `ADAPTIVE=0` (solo frames base), `FRAME_INTERVAL`, `MAX_EXTRA_PER_MIN`, `MOTION_SIGMA` (más bajo = más frames extra), `FRAME_WIDTH` (960), `JPEG_QUALITY` (80), `MAX_TOKENS`, `WORKERS`, `BUDGET_MS`, `MIN_CONF`.

## Notas
- La API key vive solo en `.env`; la app no la muestra ni la escribe en logs.
- Acceso básico (DEMO_USER) no cifra el tráfico por HTTP: limita el puerto por IP.
- Los videos subidos se borran solos a las 6 h.

