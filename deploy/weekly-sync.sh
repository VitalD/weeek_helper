#!/usr/bin/env bash
# Еженедельный цикл: задачи (API) → комментарии (Playwright) → пауза → ingest.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/rag_project}"
INGEST_DELAY_SECONDS="${INGEST_DELAY_SECONDS:-1200}"

cd "$PROJECT_DIR"
# shellcheck source=/dev/null
source "$PROJECT_DIR/.venv/bin/activate"
export PYTHONUNBUFFERED=1

log() { echo "[$(date -Is)] $*"; }

log "get_tasks (Weeek API)..."
python -m weeek_kb.parsing.get_tasks

log "get_comments (Playwright, headless)..."
python -m weeek_kb.parsing.get_comments --headless --resume

log "waiting ${INGEST_DELAY_SECONDS}s before ingest (~$((INGEST_DELAY_SECONDS / 60)) min)..."
sleep "$INGEST_DELAY_SECONDS"

log "ingest (incremental Chroma)..."
python -m weeek_kb.parsing.ingest

log "weekly-sync finished."
