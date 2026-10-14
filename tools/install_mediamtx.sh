#!/usr/bin/env bash
# Installs MediaMTX (RTSP/RTMP server) on the ECS so a phone or IP camera can PUBLISH video to it.
# Vialert then reads the stream locally (rtsp://127.0.0.1:8554/<camera>). Cameras are added from the Vialert UI.   Usage:  sudo bash tools/install_mediamtx.sh [PUBLISH_PASSWORD]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
PASS="${1:-$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)}"
TAG="${MEDIAMTX_TAG:-$(curl -fsSIL -o /dev/null -w '%{url_effective}' https://github.com/bluenviron/mediamtx/releases/latest | sed 's#.*/tag/##')}"
[ -n "$TAG" ] || { echo "Could not resolve the MediaMTX version. Re-run with MEDIAMTX_TAG=v1.x.y"; exit 1; }
ARCH=$(uname -m); case "$ARCH" in x86_64) A=amd64;; aarch64) A=arm64v8;; *) echo "arch $ARCH not supported"; exit 1;; esac
echo "MediaMTX $TAG ($A)"
mkdir -p /opt/mediamtx && cd /opt/mediamtx
curl -fL -o mm.tgz "https://github.com/bluenviron/mediamtx/releases/download/${TAG}/mediamtx_${TAG}_linux_${A}.tar.gz"
tar xzf mm.tgz mediamtx && rm mm.tgz && chmod +x mediamtx
cat > mediamtx.yml <<YML
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
  # The phone / camera publishes with this user (needs the password)
  - user: cam
    pass: ${PASS}
    ips: []
    permissions:
      - action: publish
        path:
  # Only the ECS itself (Vialert) may read the stream without password
  - user: any
    pass:
    ips: ['127.0.0.1', '::1']
    permissions:
      - action: read
        path:
paths:
  all_others:
YML
cat > /etc/systemd/system/mediamtx.service <<'UNIT'
[Unit]
Description=MediaMTX (RTSP/RTMP gateway for Vialert)
After=network.target
[Service]
WorkingDirectory=/opt/mediamtx
ExecStart=/opt/mediamtx/mediamtx /opt/mediamtx/mediamtx.yml
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now mediamtx
sleep 1; systemctl --no-pager -l status mediamtx | head -8
IP=$(curl -fs --max-time 4 https://api.ipify.org || echo "<ECS_PUBLIC_IP>")
cat <<MSG

==== Listo ====
Clave de publicacion (usuario "cam"): ${PASS}
Guardada en: /opt/mediamtx/mediamtx.yml  (si Vialert corre en esta misma maquina la lee solo; si no, ponla en .env como MTX_PUBLISH_PASS)
Abre en el firewall / grupo de seguridad (entrada): TCP 8554 (RTSP) y TCP 1935 (RTMP), idealmente solo desde las IP que transmiten.

Siguiente paso: en Vialert -> Camaras -> "+ Agregar camara" -> "Celular o equipo que transmite".
Ahi veras la URL, el usuario, la clave y un QR para la app Larix Broadcaster, y si ya llega la señal.
(Manual: rtsp://${IP}:8554/<camara>  login: cam  clave: ${PASS}   |   rtmp://${IP}:1935/<camara>?user=cam&pass=${PASS})
MSG
