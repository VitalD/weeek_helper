"""
Сбор комментариев из веб-интерфейса WEEEK (Playwright) в data/board-*.json.

Требует: pip install playwright && playwright install chromium

Пример:
  python -m weeek_kb.parsing.get_comments
  python -m weeek_kb.parsing.get_comments --dry-run --limit-tasks 2
  python -m weeek_kb.parsing.get_comments --manual-login
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from weeek_kb.config import (
    DATA_DIR,
    ROOT,
    WEEEK_EMAIL,
    WEEEK_PASSWORD,
    WEEEK_SESSION_FILE,
    WEEEK_WORKSPACE_ID,
    task_url,
)
from weeek_kb.parsing.comment_dates import (
    done_window,
    parse_comment_date,
    parse_meta_date,
    parse_task_finish_date,
)
from weeek_kb.parsing.get_tasks import META_FILENAME, load_meta_info, remove_bak_files
from weeek_kb.projects import load_projects
from weeek_kb.parsing.html_utils import strip_html
from weeek_kb.parsing.weeek_browser import (
    harvest_task_comments,
    save_session,
    start_browser,
)

logger = logging.getLogger(__name__)

ACTIVE_COLUMNS = frozenset(
    {
        "Бэклог (следующий спринт)",
        "Требуется дополнение",
        "На неделю",
        "Контроль качества",
    }
)
DONE_COLUMN = "Сделано"
DONE_STATUS = "Завершена"


def should_fetch_comments(task: dict[str, Any], anchor: date) -> bool:
    column = str(task.get("колонка") or "").strip()
    if column in ACTIVE_COLUMNS:
        return True

    status = str(task.get("статус") or "").strip()
    is_done = column == DONE_COLUMN or status == DONE_STATUS
    if not is_done:
        return False

    finished = parse_task_finish_date(task.get("датаЗавершения"))
    if finished is None:
        return False

    start, end = done_window(anchor, days=7)
    return start <= finished <= end


def _normalize_comment_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def comment_dedup_key(comment: dict[str, Any]) -> tuple[str, str]:
    d = str(comment.get("дата") or "").strip()
    body = _normalize_comment_text(str(comment.get("комментарий") or ""))
    return d, body


def dom_rows_to_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Как weeek_enrich_board_comments.dom_rows_to_comments + strip_html."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("_source") != "dom":
            continue
        raw_author = row.get("author")
        if isinstance(raw_author, str) and raw_author.strip():
            author = raw_author.strip()
        else:
            author = "—"

        parts: list[str] = []
        dh = row.get("dateHeading")
        if isinstance(dh, str) and dh.strip():
            parts.append(dh.strip())
        tm = row.get("time")
        if isinstance(tm, str) and tm.strip():
            parts.append(tm.strip())
        date_s = ", ".join(parts) if parts else ""

        text = row.get("text")
        if not isinstance(text, str):
            text = ""
        html = row.get("html")
        if isinstance(html, str) and html.strip() and not text.strip():
            text = strip_html(html)
        else:
            text = strip_html(text)

        out.append(
            {
                "автор": author,
                "дата": date_s,
                "комментарий": text.strip(),
            }
        )
    return out


def merge_comments(
    existing: list[dict[str, Any]] | None,
    new_items: list[dict[str, Any]],
    anchor: date,
) -> list[dict[str, Any]]:
    """
    С карточки добавляются все комментарии. last_date не фильтрует импорт.

    Дедуп: (дата, текст) совпадает с уже сохранённым комментарием, у которого
    дата >= anchor — повтор не добавляем. Старые в JSON (дата < anchor) не участвуют
    в дедупе, чтобы при первом прогоне подтянуть всю историю с карточки.
    """
    merged = list(existing) if isinstance(existing, list) else []

    def _dedup_key_in_recent_pool(key: tuple[str, str]) -> bool:
        for item in merged:
            if not isinstance(item, dict):
                continue
            if comment_dedup_key(item) != key:
                continue
            item_dt = parse_comment_date(item.get("дата"))
            if item_dt is None or item_dt >= anchor:
                return True
        return False

    for c in new_items:
        if not isinstance(c, dict):
            continue
        key = comment_dedup_key(c)
        if _dedup_key_in_recent_pool(key):
            continue
        merged.append(c)
    return merged


def load_board(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_board(path: Path, data: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] skip write {path}")
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def _fetched_today(task: dict[str, Any]) -> bool:
    raw = task.get("commentsFetchedAt")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()
    except ValueError:
        return False


def discover_paths(data_dir: Path, names: list[str] | None) -> list[Path]:
    if names:
        return [data_dir / n for n in names if (data_dir / n).is_file()]
    return [p.file_path for p in load_projects(data_dir)]


def apply_sidecar_to_board(
    sidecar_path: Path,
    board_path: Path,
    *,
    dry_run: bool,
) -> int:
    """Перенести последние записи из .jsonl sidecar в data/board-*.json."""
    if not sidecar_path.is_file():
        return 0

    latest: dict[int, dict[str, Any]] = {}
    with open(sidecar_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            tid = int(row["taskId"])
            latest[tid] = row

    if not latest:
        return 0

    data = load_board(board_path)
    tasks = data.get("задачи")
    if not isinstance(tasks, list):
        return 0

    updated = 0
    for task in tasks:
        if not isinstance(task, dict) or task.get("id") is None:
            continue
        tid = int(task["id"])
        row = latest.get(tid)
        if not row:
            continue
        task["comments"] = row.get("comments") or []
        if row.get("commentsFetchedAt"):
            task["commentsFetchedAt"] = row["commentsFetchedAt"]
        updated += 1

    if updated and not dry_run:
        save_board(board_path, data, dry_run=False)
    return updated


def apply_all_sidecars(
    data_dir: Path,
    *,
    files: list[str] | None = None,
    dry_run: bool = False,
    sidecar_dir: Path | None = None,
) -> int:
    sc_dir = sidecar_dir or (data_dir / ".web-comments-progress")
    if not sc_dir.is_dir():
        print(f"Нет каталога {sc_dir}", file=sys.stderr)
        return 0

    allowed_stems: set[str] | None = None
    if files:
        allowed_stems = {Path(n).stem for n in files}

    total = 0
    for sidecar_path in sorted(sc_dir.glob("*.jsonl")):
        if allowed_stems is not None and sidecar_path.stem not in allowed_stems:
            continue
        board_path = data_dir / f"{sidecar_path.stem}.json"
        if not board_path.is_file():
            print(f"Пропуск {sidecar_path.name}: нет {board_path.name}", file=sys.stderr)
            continue
        n = apply_sidecar_to_board(sidecar_path, board_path, dry_run=dry_run)
        if n:
            print(f"{board_path.name}: обновлено задач {n}", flush=True)
            total += n
    return total


def enrich_file(
    page: Any,
    path: Path,
    *,
    anchor: date,
    wait_ms: int,
    limit_tasks: int,
    dry_run: bool,
    save_every: int,
    sidecar_dir: Path,
    only_task_id: int | None,
    resume: bool,
    force_refresh: bool,
    replace_comments: bool,
    progress_sidecar: bool,
) -> tuple[int, int, int]:
    data = load_board(path)
    tasks = data.get("задачи")
    if not isinstance(tasks, list):
        print(f"Пропуск {path}: нет массива задачи[]", file=sys.stderr)
        return 0, 0, 0

    eligible: list[int] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict) or task.get("id") is None:
            continue
        if only_task_id is not None and int(task["id"]) != only_task_id:
            continue
        if should_fetch_comments(task, anchor):
            eligible.append(i)

    if only_task_id is not None and not eligible:
        print(f"Задача id={only_task_id} не найдена или не подходит под фильтр в {path}", file=sys.stderr)
        return 0, 0, 0

    sidecar_path: Path | None = None
    if progress_sidecar and not dry_run:
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = sidecar_dir / f"{path.stem}.jsonl"
        print(f"  -> progress-log: {sidecar_path}", flush=True)
    n_done = 0
    n_skipped = 0
    n_filtered_out = len(tasks) - len(eligible) if only_task_id is None else 0
    total_new = 0

    for idx in eligible:
        if limit_tasks and n_done >= limit_tasks:
            break
        task = tasks[idx]
        tid = int(task["id"])

        if resume and not force_refresh and _fetched_today(task):
            n_skipped += 1
            continue

        url = task_url(tid)
        print(f"  задача {tid}: {url}", flush=True)
        raw_rows = harvest_task_comments(page, url, wait_ms)
        parsed = dom_rows_to_comments(raw_rows)
        existing_list = (
            task.get("comments") if isinstance(task.get("comments"), list) else []
        )

        before = len(existing_list)
        if replace_comments:
            task["comments"] = parsed
        else:
            task["comments"] = merge_comments(existing_list, parsed, anchor)
        added = len(task["comments"]) - before
        total_new += max(0, added)
        task["commentsFetchedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        n_done += 1

        skipped = max(0, len(parsed) - added) if not replace_comments else 0
        print(
            f"  task {tid}: DOM={len(raw_rows)}, с карточки={len(parsed)}, "
            f"+{added} новых (всего {len(task['comments'])}, пропущено дублей {skipped})",
            flush=True,
        )
        if dry_run and parsed:
            print(
                f"  [dry-run] в JSON не пишем; без --dry-run будет {len(task['comments'])} коммент.",
                flush=True,
            )

        if not dry_run:
            save_board(path, data, dry_run=False)
            print(f"  -> записан {path.name}", flush=True)

        if not dry_run and sidecar_path is not None:
            line = json.dumps(
                {
                    "taskId": tid,
                    "comments": task["comments"],
                    "commentsFetchedAt": task["commentsFetchedAt"],
                },
                ensure_ascii=False,
            )
            with sidecar_path.open("a", encoding="utf-8") as sf:
                sf.write(line + "\n")

    if n_skipped:
        print(f"  -> пропущено (resume, уже сегодня): {n_skipped}", flush=True)

    return n_done, total_new, n_filtered_out


def fetch_comments(
    data_dir: Path,
    *,
    anchor: date,
    dry_run: bool = False,
    headless: bool = False,
    wait_ms: int = 2000,
    limit_tasks: int = 0,
    only_task_id: int | None = None,
    files: list[str] | None = None,
    manual_login: bool = False,
    cookie: str = "",
    session_file: Path | None = None,
    no_save_session: bool = False,
    resume: bool = False,
    force_refresh: bool = False,
    save_every: int = 1,
    sidecar_dir: Path | None = None,
    replace_comments: bool = False,
    progress_sidecar: bool = False,
) -> None:
    paths = discover_paths(data_dir, files)
    if not paths:
        raise SystemExit("Нет файлов board-*.json")

    ws_id = int(WEEEK_WORKSPACE_ID)
    sc_dir = sidecar_dir or (data_dir / ".web-comments-progress")
    session_path = session_file or WEEEK_SESSION_FILE

    email = (os.getenv("WEEEK_EMAIL") or os.getenv("WEEEK_LOGIN") or WEEEK_EMAIL).strip()
    password = (os.getenv("WEEEK_PASSWORD") or WEEEK_PASSWORD).strip()

    pw = browser = context = page = None
    try:
        pw, browser, context, page = start_browser(
            ws_id=ws_id,
            headless=headless,
            cookie_header=cookie,
            email=email,
            password=password,
            manual_login=manual_login,
            session_file=session_path,
        )
        print("Браузер готов, начинаем обход задач…", flush=True)

        start, end = done_window(anchor, days=7)
        print(
            f"Якорная дата: {anchor.strftime('%d.%m.%Y')}; "
            f"закрытые задачи: {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}",
            flush=True,
        )

        grand_tasks = 0
        grand_new = 0
        for path in paths:
            print(f"=== {path.name} ===", flush=True)
            t, c, _ = enrich_file(
                page,
                path,
                anchor=anchor,
                wait_ms=wait_ms,
                limit_tasks=limit_tasks,
                dry_run=dry_run,
                save_every=save_every,
                sidecar_dir=sc_dir,
                only_task_id=only_task_id,
                resume=resume,
                force_refresh=force_refresh,
                replace_comments=replace_comments,
                progress_sidecar=progress_sidecar,
            )
            grand_tasks += t
            grand_new += c
            print(f"  обработано задач: {t}, новых комментариев: {c}", flush=True)

        print(f"Итого задач: {grand_tasks}, добавлено комментариев: {grand_new}", flush=True)
        n_bak = remove_bak_files(data_dir)
        if n_bak:
            print(f"Удалено .bak: {n_bak}", flush=True)
    finally:
        if context is not None and not dry_run and not no_save_session and session_path:
            try:
                save_session(context, session_path)
                print(f"Сессия сохранена: {session_path}", flush=True)
            except Exception as e:
                print(f"Не удалось сохранить сессию: {e}", file=sys.stderr)
        for obj in (page, context, browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Комментарии из веб-UI WEEEK в data/board-*.json (Playwright)",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Имена board-*.json (по умолчанию все)",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=2000)
    parser.add_argument("--limit-tasks", type=int, default=0)
    parser.add_argument("--only-task-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manual-login", action="store_true")
    parser.add_argument("--cookie", default="")
    parser.add_argument("--cookie-file", type=Path, default=None)
    parser.add_argument("--session-file", type=Path, default=None)
    parser.add_argument("--no-save-session", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--save-every", type=int, default=1)
    parser.add_argument("--sidecar-dir", type=Path, default=None)
    parser.add_argument(
        "--progress-sidecar",
        action="store_true",
        help="Дополнительно писать progress-log в data/.web-comments-progress/*.jsonl",
    )
    parser.add_argument(
        "--apply-sidecar",
        action="store_true",
        help="Перенести комментарии из .web-comments-progress в data/board-*.json (без браузера)",
    )
    parser.add_argument(
        "--replace-comments",
        action="store_true",
        help="Заменить comments[] целиком с карточки (как RNP), без merge",
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=True)

    if args.apply_sidecar:
        n = apply_all_sidecars(
            args.data_dir,
            files=args.files,
            dry_run=args.dry_run,
            sidecar_dir=args.sidecar_dir,
        )
        print(f"Готово: обновлено задач в board-*.json: {n}", flush=True)
        n_bak = remove_bak_files(args.data_dir)
        if n_bak:
            print(f"Удалено .bak: {n_bak}", flush=True)
        return

    cookie = args.cookie
    if args.cookie_file and args.cookie_file.is_file():
        cookie = args.cookie_file.read_text(encoding="utf-8").strip()

    meta = load_meta_info(args.data_dir)
    last_date_raw = meta.get("last_date")
    if not last_date_raw:
        raise SystemExit(f"В {META_FILENAME} нет поля last_date")
    anchor = parse_meta_date(str(last_date_raw))

    fetch_comments(
        args.data_dir,
        anchor=anchor,
        dry_run=args.dry_run,
        headless=args.headless,
        wait_ms=args.wait_ms,
        limit_tasks=args.limit_tasks,
        only_task_id=args.only_task_id,
        files=args.files,
        manual_login=args.manual_login,
        cookie=cookie,
        session_file=args.session_file,
        no_save_session=args.no_save_session,
        resume=args.resume,
        force_refresh=args.force_refresh,
        save_every=args.save_every,
        sidecar_dir=args.sidecar_dir,
        replace_comments=args.replace_comments,
        progress_sidecar=args.progress_sidecar,
    )


if __name__ == "__main__":
    main()
