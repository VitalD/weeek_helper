# Деплой на VPS (`/root/rag_project`)

Пошаговый чеклист. **`.env` не копировать в git, не отправлять в чаты с ИИ.**

## 0. Сохранить данные перед очисткой

```bash
cp -a /root/rag_project/.env /root/weeek-env.backup
cp -a /root/rag_project/data /root/weeek-data.backup
```

## 1. Остановить старого бота

Попробуйте по очереди (что сработает — то и было):

```bash
systemctl stop weeek-kb-bot
systemctl stop rag-bot
systemctl stop weeek-bot
pkill -f "weeek_kb.bot" || true
pkill -f "rag_project" || true
screen -ls    # если есть screen-сессия — screen -X -S <name> quit
```

Проверка, что процесс не висит:

```bash
pgrep -af "weeek_kb|rag_project|bot.py" || echo "процессов нет"
```

## 2. Обновить код (сохранить `.env` и `data/`)

```bash
cd /root
# Удалить только код, не трогая бэкапы:
rm -rf /root/rag_project/weeek_kb /root/rag_project/deploy /root/rag_project/config \
  /root/rag_project/tests /root/rag_project/requirements.txt /root/rag_project/README.md

# Загрузить с машины разработки (пример rsync с ноутбука):
# rsync -avz --exclude '.venv' --exclude 'data' --exclude 'chroma_db' --exclude '.env' --exclude '.git' \
#   ./weeek_helper/ root@YOUR_SERVER:/root/rag_project/

# Или git clone / pull в /root/rag_project
```

Восстановить `.env` и `data/`, если удаляли всё каталогом:

```bash
cp -a /root/weeek-env.backup /root/rag_project/.env
cp -a /root/weeek-data.backup /root/rag_project/data
```

## 3. Виртуальное окружение

```bash
cd /root/rag_project
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install chromium
mkdir -p data
# если нет meta-info.json:
test -f data/meta-info.json || cp config/meta-info.json.example data/meta-info.json
```

## 4. Разовый прогон: задачи и комментарии

```bash
cd /root/rag_project && source .venv/bin/activate

python -m weeek_kb.parsing.get_tasks
python -m weeek_kb.parsing.get_comments --headless --resume
# при первом входе или протухшей сессии:
# python -m weeek_kb.parsing.get_comments --headless --manual-login
```

Проверка: в `data/` обновились `board-*.json` и `meta-info.json`.

## 5. Векторная база (первый раз)

```bash
systemctl stop weeek-kb-bot 2>/dev/null || true
rm -rf /root/rag_project/chroma_db/*
bash deploy/initial-full-index.sh
```

Число подкаталогов в `chroma_db/` ≈ числу `board-*.json` в `data/` (допустимо +1, не в 2–3 раза больше).

## 6. Systemd: бот + еженедельная синхронизация

```bash
bash deploy/install-systemd.sh /root/rag_project
```

Таймер: **воскресенье 03:00** — `get_tasks` → `get_comments` → пауза 20 мин → `ingest`.

Разовый запуск недельного цикла (тест):

```bash
sudo systemctl start weeek-kb-weekly-sync.service
journalctl -u weeek-kb-weekly-sync.service -f
```

## 7. Проверка бота

```bash
systemctl status weeek-kb-bot
tail -f /root/rag_project/weeek_kb.log
```

- В Telegram: вопрос по задаче за последнюю неделю (должен найти в комментариях/описании).
- Голосовое: «Поставь задачу на сайте …» — сценарий постановки задачи.

## 8. Фоновая работа после закрытия SSH

Бот и таймеры работают через **systemd**, сессия SSH не нужна:

```bash
systemctl is-active weeek-kb-bot
systemctl list-timers | grep weeek
```

Сообщение в общий чат (пример):

> Обновлён Weeek-бот: свежие задачи/комментарии, переиндексация Chroma по воскресеньям ~03:00. Поиск по базе и постановка задач (в т.ч. голосом) работают.

## Устранение неполадок

| Проблема | Действие |
|----------|----------|
| Playwright не логинится | `WEEEK_EMAIL`/`WEEEK_PASSWORD` в `.env`; `--manual-login` |
| Пустой поиск | `ingest --reset`; проверить `active-board-ids.json` |
| Много папок в `chroma_db/` | `rm -rf chroma_db/*` и `ingest --reset` при остановленном боте |
