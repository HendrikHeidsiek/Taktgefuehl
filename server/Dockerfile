# deno runs YouTube's player JavaScript for yt-dlp (needed to fetch audio for the sound analysis).
FROM denoland/deno:bin-2.9.4 AS deno

# 3.14: essentia (sound analysis) is only published for it.
FROM python:3.14-slim
COPY --from=deno /deno /usr/local/bin/deno

LABEL org.opencontainers.image.title="Taktgefühl Server" \
      org.opencontainers.image.description="Erkennt Hör-Phasen im YouTube-Music-Verlauf und pflegt Mix-Playlists. Bedienung über die Taktgefühl-App." \
      org.opencontainers.image.version="0.2.10"

# Start page: own address and the optional "Betreiber dieser Seite" line (release.sh passes them in).
ARG PUBLIC_URL=""
ARG OPERATOR_NAME=""
ARG OPERATOR_URL=""

# Everything below can be changed in the NAS Docker UI when creating the container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PUBLIC_URL="${PUBLIC_URL}" \
    OPERATOR_NAME="${OPERATOR_NAME}" \
    OPERATOR_URL="${OPERATOR_URL}" \
    PYTHONUNBUFFERED=1 \
    ADMIN_PASSWORD="" \
    ANTHROPIC_API_KEY="" \
    AI_MODEL="claude-opus-5-5" \
    SYNC_MINUTES="30" \
    REBUILD_HOUR="5" \
    TZ="Europe/Berlin" \
    PUID="" \
    PGID="" \
    PORT="8080" \
    DATA_PATH="/data" \
    MODEL_PATH="/srv/models" \
    SOUND="1" \
    SOUND_THREADS="2" \
    XDG_CACHE_HOME="/tmp/cache" \
    DENO_DIR="/tmp/deno"

# libatomic: needed by essentia's compiled library
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Discogs-EffNet as ONNX (sound fingerprints) and the names of its 400 styles, from the Essentia project.
RUN mkdir -p /srv/models && python -c "import json, urllib.request as u; \
b = 'https://essentia.upf.edu/models/'; \
u.urlretrieve(b + 'feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx', '/srv/models/discogs-effnet-bsdynamic-1.onnx'); \
c = json.load(u.urlopen(b + 'classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json'))['classes']; \
json.dump(c, open('/srv/models/discogs400-styles.json', 'w'))"
COPY app ./app
# Android app for the update hint (built from ../app by release.sh).
COPY download ./download
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080'))" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
