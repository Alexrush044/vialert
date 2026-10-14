#!/bin/sh
# Genera /mediamtx.yml: los celulares/equipos PUBLICAN con MTX_PUBLISH_USER/PASS; Vialert LEE con MTX_READ_USER/PASS.
set -e
: "${MTX_PUBLISH_USER:=cam}"
: "${MTX_READ_USER:=vialert}"
: "${MTX_PUBLISH_PASS:?Define MTX_PUBLISH_PASS en .env}"
: "${MTX_READ_PASS:?Define MTX_READ_PASS en .env}"
for v in "$MTX_PUBLISH_USER" "$MTX_PUBLISH_PASS" "$MTX_READ_USER" "$MTX_READ_PASS"; do
  case "$v" in *[!A-Za-z0-9_.-]*|"") echo "Usuarios y claves: solo letras, numeros, _ . -"; exit 1;; esac
done
cat > /mediamtx.yml <<YML
logLevel: info
api: no
metrics: no
pprof: no
playback: no
hls: no
webrtc: no
srt: no
rtsp: yes
rtspAddress: :8554
rtmp: yes
rtmpAddress: :1935
authMethod: internal
authInternalUsers:
  - user: ${MTX_PUBLISH_USER}
    pass: ${MTX_PUBLISH_PASS}
    ips: []
    permissions:
      - action: publish
        path:
  - user: ${MTX_READ_USER}
    pass: ${MTX_READ_PASS}
    ips: []
    permissions:
      - action: read
        path:
paths:
  all_others:
YML
exec /mediamtx /mediamtx.yml
