FROM python:3.13-slim-bookworm AS receivers
ARG TARGETARCH
RUN test "${TARGETARCH:-$(dpkg --print-architecture)}" = amd64 \
    || (echo 'The pinned Spotify receiver requires linux/amd64.' >&2; exit 1)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates build-essential autoconf automake libtool pkg-config flex bison texinfo \
    libavahi-client-dev libssl-dev libsoxr-dev libpopt-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*
COPY server/spotify/install_receiver.py /build/spotify/install_receiver.py
COPY server/airplay/build_receiver.py server/airplay/audio_sink.c /build/airplay/
RUN python /build/spotify/install_receiver.py /opt/receivers/spotify \
    && python /build/airplay/build_receiver.py /opt/receivers/airplay

FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PUID=10001 PGID=10001 HOME=/data \
    SINK_BIND_ADDRESS=0.0.0.0 SINK_HTTP_PORT=8080 SINK_TCP_PORT=8765 \
    SINK_SPOTIFY_ENABLED=true SINK_AIRPLAY_ENABLED=true SINK_SETUP_HTTPS_ENABLED=false
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates openssl ffmpeg librubberband2 libsamplerate0 \
    libavahi-client3 libssl3 libsoxr0 libpopt0 avahi-daemon dbus tini \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/machine-id /var/lib/dbus/machine-id \
    && ln -s /run/machine-id /etc/machine-id \
    && ln -s /run/machine-id /var/lib/dbus/machine-id
# Runtime identity files keep configurable UID/GID compatible with D-Bus auth
# while the image filesystem remains read-only.
RUN mv /etc/passwd /etc/passwd.base && ln -s /run/passwd /etc/passwd \
    && mv /etc/group /etc/group.base && ln -s /run/group /etc/group
WORKDIR /app
COPY server/requirements.txt server/requirements.lock /app/server/
RUN python -m pip install --no-cache-dir -r /app/server/requirements.txt
COPY --from=receivers /opt/receivers /opt/receivers
COPY server /app/server
COPY VERSION /app/VERSION
COPY frontend /app/frontend
COPY docker /app/docker
RUN mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data
LABEL org.opencontainers.image.title="ESP32 Wi-Fi sink" \
      org.opencontainers.image.description="Network audio server and ESP32 setup interface"
VOLUME ["/data"]
EXPOSE 8080/tcp 8765/tcp 8443/tcp
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/docker/entrypoint.py"]
