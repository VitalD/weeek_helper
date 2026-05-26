#!/usr/bin/env bash
# Установка systemd-юнитов бота и еженедельной синхронизации.
set -euo pipefail

PROJECT_DIR="${1:-/root/rag_project}"

if [ ! -d "$PROJECT_DIR/.venv" ]; then
  echo "Нет $PROJECT_DIR/.venv — сначала создайте venv и pip install -r requirements.txt"
  exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "Нет $PROJECT_DIR/.env — создайте файл с токенами (не коммитить в git)."
  exit 1
fi

chmod +x "$PROJECT_DIR/deploy/weekly-sync.sh" "$PROJECT_DIR/deploy/initial-full-index.sh"

render() {
  local src="$1" dest="$2"
  sed "s|@PROJECT_DIR@|${PROJECT_DIR}|g" "$src" >"$dest"
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

for unit in weeek-kb-bot.service weeek-kb-weekly-sync.service weeek-kb-weekly-sync.timer; do
  render "$PROJECT_DIR/deploy/$unit" "$TMP/$unit"
  sudo cp "$TMP/$unit" "/etc/systemd/system/$unit"
done

# Старый ежедневный ingest-таймер (если был) — отключаем
if systemctl list-unit-files weeek-kb-sync.timer &>/dev/null; then
  sudo systemctl disable --now weeek-kb-sync.timer 2>/dev/null || true
fi

sudo systemctl daemon-reload
sudo systemctl enable --now weeek-kb-bot.service
sudo systemctl enable --now weeek-kb-weekly-sync.timer

echo "OK: weeek-kb-bot + weeek-kb-weekly-sync.timer (воскресенье 03:00)"
echo "  systemctl status weeek-kb-bot"
echo "  systemctl list-timers weeek-kb-weekly-sync.timer"
