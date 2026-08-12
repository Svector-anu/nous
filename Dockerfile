# One python process serving a websocket and a single html file. No build step, no node,
# no bundler — the viewer is plain files and three.js is vendored, so there is nothing to
# compile and nothing to fetch at runtime.
FROM python:3.14-slim

# Bytecode caching off and unbuffered stdout: logs need to reach the platform as they
# happen, not when a buffer fills, or a crash takes its own explanation with it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Requirements first so a code change does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

# The world lives here. This path must be the volume mount point or every deploy starts a
# new civilization at day 1 — src/api/server.py resolves DEFAULT_DB_PATH to <repo>/data.
#
# Deliberately no VOLUME instruction: railway rejects it outright ("docker VOLUME is not
# supported, use Railway Volumes"), and every platform worth deploying to declares mounts
# in its own config rather than in the image. the directory just needs to exist.
RUN mkdir -p /app/data

EXPOSE 8080

# /health returns 200 only while the tick loop is actually running, so a frozen simulation
# behind a live http server is reported as unhealthy rather than as fine.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "src.main"]
