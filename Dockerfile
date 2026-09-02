# syntax=docker/dockerfile:1

# Build the SPA separately so Node and its dependencies are not in the runtime
# image. Node 22 matches the frontend toolchain's declared Node typings.
FROM node:22.14.0-bookworm-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM debian:trixie-slim

# ---------------------------------------------------------------------------
# System packages
#   - i386 foreign arch: steamcmd is a 32-bit binary
#   - lib32gcc-s1 / libcurl4 / libssl3 / net-tools: Reforger + steamcmd runtime
#   - gosu: drop privileges from the root entrypoint to the steam user
#   - tini: PID 1 reaping for the game child processes
#   - python3 / venv / pip: the backend
#   - locales / tzdata: predictable locale + timezone
# ---------------------------------------------------------------------------
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        lib32gcc-s1 \
        libcurl4 \
        libssl3 \
        net-tools \
        ca-certificates \
        passwd \
        gosu \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        tini \
        locales \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# steam user/group (uid/gid 1000). The entrypoint remaps these at runtime to
# match the bind-mount owner; 1000 is just the build-time default.
# ---------------------------------------------------------------------------
RUN groupadd --gid 1000 steam \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/steam --shell /bin/sh steam

# ---------------------------------------------------------------------------
# steamcmd into /home/steam/steamcmd
# ---------------------------------------------------------------------------
RUN mkdir -p /home/steam/steamcmd \
    && curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
        | tar -xz -C /home/steam/steamcmd \
    && chown -R steam:steam /home/steam/steamcmd

# ---------------------------------------------------------------------------
# Python virtualenv at /opt/venv. requirements.txt copied first so the pip
# layer is cached independently of the backend source.
# ---------------------------------------------------------------------------
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    TZ=Europe/Zurich \
    PYTHONUNBUFFERED=1

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/backend/requirements.txt

# ---------------------------------------------------------------------------
# Application code + entrypoint
# ---------------------------------------------------------------------------
COPY backend/ /app/backend/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --from=frontend-build /frontend/dist /app/frontend/dist
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app/backend

EXPOSE 18090

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/opt/venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18090", "--app-dir", "/app/backend"]
