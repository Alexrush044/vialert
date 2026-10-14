# MediaMTX (pasarela RTSP/RTMP) con la configuracion generada desde variables de entorno.
FROM bluenviron/mediamtx:1 AS src
FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=src /mediamtx /mediamtx
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8554 1935
ENTRYPOINT ["/entrypoint.sh"]
