FROM denoland/deno:bin AS deno_bin
FROM python:3.13-slim

ARG BGUTIL_VERSION=2.0.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOME=/tmp/yt-dlp-home \
    XDG_CACHE_HOME=/tmp/yt-dlp-cache \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1 \
    BGUTIL_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server \
    YOUTUBE_PLAYER_CLIENT=mweb

COPY --from=deno_bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates git \
      iptables iproute2 wireguard-tools gosu util-linux \
    && rm -rf /var/lib/apt/lists/*

# Docker /proc/sys may be read-only when src_valid_mark is already 1.
# Fail the build if the version of wg-quick changes unexpectedly.
RUN python -c 'from pathlib import Path; p=Path("/usr/bin/wg-quick"); s=p.read_text(); old="[[ $proto == -4 ]] && cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1"; new="[[ $proto == -4 && $(sysctl -n net.ipv4.conf.all.src_valid_mark) != 1 ]] && cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1"; assert old in s or "sysctl -n net.ipv4.conf.all.src_valid_mark" in s, "Unknown wg-quick sysctl handling"; p.write_text(s.replace(old, new))'

RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && DENO_DIR=/opt/bgutil-deno-cache deno install --allow-scripts=npm:canvas --frozen \
    && chmod -R a+rX /opt/bgutil-ytdlp-pot-provider /opt/bgutil-deno-cache

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime is normally an arbitrary non-root UID/GID from Compose. Some Python
# packages (notably spotDL) create config/cache data under HOME on first import.
# Recreate these temp roots after package installation so no build-time root-owned
# directories can block the runtime user.
RUN rm -rf "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR" \
    && mkdir -p "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR" \
    && chmod 1777 "$HOME" "$XDG_CACHE_HOME" "$DENO_DIR"

COPY app.py staging.py album_completeness.py album_submission.py job_diagnostics.py disk_inventory.py job_history.py download_resilience.py retry_queue.py surfshark_vpn.py vpn_ui.py vpn_gateway.py youtube_cookies.py library_repair.py extension_package.py album_failure.py container_runtime.py ./
COPY chrome-extension ./chrome-extension
COPY templates ./templates
COPY static ./static

EXPOSE 4545

HEALTHCHECK --interval=30s --timeout=4s --start-period=40s --retries=3 \
  CMD python -c "import socket,urllib.request; s=socket.create_connection(('127.0.0.1',8787),timeout=1);s.close();urllib.request.urlopen('http://127.0.0.1:4545/health',timeout=2)"

# PID 1 supervises both OS processes; a failed one stops the other.
CMD ["python", "-u", "/app/container_runtime.py"]
