#!/usr/bin/env bash
# Первый полный индекс после пустого chroma_db/ (один раз вручную).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/rag_project}"

cd "$PROJECT_DIR"
# shellcheck source=/dev/null
source "$PROJECT_DIR/.venv/bin/activate"
export PYTHONUNBUFFERED=1

if [ -d chroma_db ] && [ -n "$(ls -A chroma_db 2>/dev/null)" ]; then
  echo "chroma_db/ не пустая. Остановите бота и очистите каталог, затем повторите:"
  echo "  systemctl stop weeek-kb-bot"
  echo "  rm -rf chroma_db/*"
  exit 1
fi

python -m weeek_kb.parsing.ingest --reset
