# syntax=docker/dockerfile:1.7
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS shell-builder
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY shell_src/ shell_src/
COPY tools/build_shell.mjs tools/build_shell.mjs
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS python-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY pyproject.toml LICENSE THIRD-PARTY-NOTICES.md README.md ./
COPY LICENSES/ LICENSES/
COPY ankiweb/ ankiweb/
COPY tools/fetch_web_assets.py tools/fetch_web_assets.py
COPY --from=shell-builder /src/ankiweb/shell/static/bootstrap.js ankiweb/shell/static/bootstrap.js
RUN python tools/fetch_web_assets.py
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip==26.2.1 \
    && /opt/venv/bin/pip install .

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data/home \
    TMPDIR=/data/tmp \
    ANKIWEB_COLLECTION=/data/anki/collection.anki2 \
    ANKIWEB_IMPORT_TMP_DIR=/data/import-tmp \
    ANKIWEB_HOST=0.0.0.0 \
    ANKIWEB_PORT=8000
RUN groupadd --gid 10001 ankiweb \
    && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home ankiweb \
    && mkdir -p /data/home /data/anki /data/import-tmp /data/backups /data/tmp \
    && chown -R ankiweb:ankiweb /data
COPY --from=python-builder /opt/venv /opt/venv
# web_assets are generated and gitignored, so copy them explicitly into installed package.
COPY --from=python-builder /src/ankiweb/web_assets /opt/venv/lib/python3.12/site-packages/ankiweb/web_assets
COPY --from=python-builder /src/ankiweb/shell /opt/venv/lib/python3.12/site-packages/ankiweb/shell
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3))['ok']"
ENTRYPOINT ["python", "-m", "ankiweb"]
