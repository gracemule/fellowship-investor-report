# Chui Ventures reporter: FastAPI app + the LangGraph agent + LibreOffice for Word -> PDF.
#
# Brand fonts (Larken) are licensed and are NOT in this image or the repository. They arrive with
# the user's synced Branding folder and are installed per-run (see render/fonts.py).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/app \
    CHUI_WORKDIR=/var/chui \
    CHUI_ENV=production

# LibreOffice (headless Word -> PDF), fontconfig for font discovery, tini to reap the zombie
# soffice processes LibreOffice is known to leave behind.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-core fontconfig fonts-dejavu-core fonts-liberation \
      ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /var/chui /opt/venv /app \
 && chown -R app:app /var/chui /opt/venv /app /home/app

WORKDIR /app
USER app

RUN pip install --user uv && ln -s /home/app/.local/bin/uv /home/app/uv
COPY --chown=app:app pyproject.toml uv.lock README.md ./
COPY --chown=app:app src ./src
RUN /home/app/uv sync --frozen --no-dev --no-editable

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "chui_reporter.app"]
