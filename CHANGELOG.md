# Changelog

## 1.1.0
- **Cola de OBS en el servidor**: cada video que avisa FunctionGraph se registra en un libro persistente (`events/_ingest.json`), entra a una cola y se analiza solo, uno tras otro (`INGEST_WORKERS`), sin navegador abierto. Estados: en cola → descargando → analizando (con %) → completo / error; reintento de descarga, botón **Reintentar**, y retoma lo pendiente si el contenedor se reinicia. Subir muchos videos de golpe es seguro.
- **YOLO detallado**: modelo `yolo11s` por defecto, 800 px, confianza 0.25, 3 muestras/s; nuevas clases (perro, caballo, vaca, cuchillo, bate) y reglas (vehículos encimados con frenado brusco, persona encimada con vehículo, animal, cuchillo/bate junto a persona); umbrales más sensibles; más frames extra (`MAX_EXTRA_PER_MIN=14`).
- **Detector local de fuego y humo** (`fire.py`, color + parpadeo): YOLO no tiene esas clases; ahora un posible incendio pide un frame extra al VLM sin esperar la cuadrícula base.
- **Los pesos de YOLO viajan en el repo** (`models/`): ya no dependen de que la ECS alcance GitHub (causa de "ya no detecta" en instalaciones nuevas). YOLO se prueba al arrancar; si falla se ve un aviso rojo en la interfaz, en el log y en `selfcheck`.
- La pista que recibe el VLM ahora advierte que el detector no reconoce fuego, humo ni choques.
- **Todo en español** (el inglés queda apagado salvo `VIALERT_ALLOW_EN=1`).
- **Registros** con fuente (cámara u OBS/archivo), fecha, hora y segundo dentro del video; un incidente nunca mezcla fuentes; CSV con columnas en español.
- Webhook incluye `source_kind`.

## 1.0.0
- Cámaras dinámicas: alta, edición y baja desde la interfaz (sin límite fijo; `CAM_MAX`).
- Asistente "Agregar cámara": celular/equipo que transmite (URL, usuario, clave, QR para Larix, detección de señal e inicio automático) y cámara IP (presets de marca, prueba de conexión con diagnóstico).
- Vista **Registros**: consulta global por cámara, fechas, evento, severidad, persona caída y texto; matriz día × cámara; CSV.
- Webhook de incidentes (`WEBHOOK_URL`).
- Escenario 2: video en OBS → FunctionGraph → `/api/ingest` → resultado en OBS.
- Bitácora persistente por cámara y por día, multi-suscriptor SSE, MJPEG con cajas de YOLO.
- Docker (Vialert + MediaMTX), `/healthz`, `tools/selfcheck.py`, pruebas de humo.
