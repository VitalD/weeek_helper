"""Загрузка новых задач из Weeek API в data/board-*.json (с last_id из meta-info.json).

Описания очищаются от HTML (weeek_kb.parsing.html_utils) перед записью в JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from weeek_kb.add.weeek_api import (
    WeeekApiError,
    get_board_columns,
    get_task,
    list_tasks_page,
)
from weeek_kb.config import DATA_DIR
from weeek_kb.parsing.html_utils import sanitize_task_record
from weeek_kb.projects import Project, load_projects

logger = logging.getLogger(__name__)

META_FILENAME = "meta-info.json"


def remove_bak_files(data_dir: Path) -> int:
    """Удалить резервные копии board-*.json.bak в каталоге data."""
    removed = 0
    for path in sorted(data_dir.glob("*.bak")):
        path.unlink()
        removed += 1
    return removed


def load_meta_info(data_dir: Path) -> dict[str, Any]:
    path = data_dir / META_FILENAME
    if not path.is_file():
        raise SystemExit(f"Нет файла {path}. Создайте meta-info.json с полями last_id и last_date.")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "last_id" not in data:
        raise SystemExit(f"В {path} отсутствует поле last_id")
    return data


def save_meta_info(data_dir: Path, last_id: int, last_date: str) -> None:
    path = data_dir / META_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_id": last_id, "last_date": last_date}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def max_task_id_in_data(data_dir: Path) -> int:
    """Максимальный id задачи в активных board-*.json."""
    max_id = 0
    for p in load_projects(data_dir):
        with open(p.file_path, encoding="utf-8") as f:
            doc = json.load(f)
        for task in doc.get("задачи") or []:
            if isinstance(task, dict) and task.get("id") is not None:
                max_id = max(max_id, int(task["id"]))
    return max_id


def _sync_date_label() -> str:
    return date.today().strftime("%d.%m.%Y")


def finalize_meta_info(data_dir: Path, *, dry_run: bool) -> tuple[int, str]:
    """
    После синхронизации записать в meta-info.json последний id в data/ и дату прогона.
    Возвращает (last_id, last_date).
    """
    last_id = max_task_id_in_data(data_dir)
    last_date = _sync_date_label()
    if not dry_run:
        save_meta_info(data_dir, last_id, last_date)
    return last_id, last_date


def _iso_to_date_str(iso: str | None) -> str | None:
    if not iso:
        return None
    s = str(iso).strip()
    if not s:
        return None
    if "T" in s:
        return s[:10]
    return s


def _column_map(board_id: int, cache: dict[int, dict[int, str]]) -> dict[int, str]:
    if board_id not in cache:
        cols = get_board_columns(board_id)
        cache[board_id] = {int(c["id"]): str(c.get("name") or "") for c in cols if c.get("id") is not None}
    return cache[board_id]


def api_task_to_export(
    task: dict[str, Any],
    *,
    column_names: dict[int, str],
) -> dict[str, Any]:
    col_id = task.get("boardColumnId")
    column = column_names.get(int(col_id), "") if col_id is not None else ""

    completed = bool(task.get("isCompleted"))
    status = "Завершена" if completed else "Активна"

    finished: str | None = None
    if completed:
        finished = _iso_to_date_str(task.get("dateEnd")) or _iso_to_date_str(task.get("updatedAt"))

    export: dict[str, Any] = {
        "id": int(task["id"]),
        "название": str(task.get("title") or "").strip(),
        "описание": str(task.get("description") or ""),
        "статус": status,
        "колонка": column,
        "датаСоздания": str(task.get("createdAt") or ""),
        "датаЗавершения": finished,
        "comments": [],
    }
    sanitize_task_record(export)
    return export


def empty_board_ids(projects: list[Project]) -> set[int]:
    """Доски без задач в JSON — для них догружаем все задачи с API, не только id > last_id."""
    empty: set[int] = set()
    for p in projects:
        with open(p.file_path, encoding="utf-8") as f:
            doc = json.load(f)
        tasks = doc.get("задачи") or []
        if not tasks:
            empty.add(p.board_id)
    return empty


def collect_candidate_ids(
    project_ids: set[int],
    board_ids: set[int],
    *,
    after_id: int,
    backfill_board_ids: set[int] | None = None,
) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    backfill = backfill_board_ids or set()

    for project_id in sorted(project_ids):
        offset = 0
        pages = 0
        while pages < 500:
            data = list_tasks_page(project_id, offset=offset)
            batch = data.get("tasks") or []
            if not batch:
                break
            for row in batch:
                if not isinstance(row, dict):
                    continue
                tid = int(row["id"])
                bid = int(row.get("boardId") or 0)
                if bid not in board_ids:
                    continue
                if bid not in backfill and tid <= after_id:
                    continue
                if tid not in seen:
                    seen.add(tid)
                    ordered.append(tid)
            if not data.get("hasMore"):
                break
            offset += len(batch)
            pages += 1

    ordered.sort()
    return ordered


def merge_tasks_into_boards(
    projects: list[Project],
    exports_by_board: dict[int, list[dict[str, Any]]],
    *,
    dry_run: bool,
) -> dict[int, int]:
    board_to_path = {p.board_id: p.file_path for p in projects}
    written: dict[int, int] = {}

    for board_id, new_tasks in exports_by_board.items():
        path = board_to_path.get(board_id)
        if not path:
            logger.warning("Нет board-*.json для boardId=%s, пропуск %s задач", board_id, len(new_tasks))
            continue

        with open(path, encoding="utf-8") as f:
            doc = json.load(f)

        existing_ids = {
            int(t["id"])
            for t in doc.get("задачи") or []
            if isinstance(t, dict) and t.get("id") is not None
        }

        by_id: dict[int, dict[str, Any]] = {}
        for t in doc.get("задачи") or []:
            if isinstance(t, dict) and t.get("id") is not None:
                by_id[int(t["id"])] = t

        for t in new_tasks:
            sanitize_task_record(t)
            by_id[int(t["id"])] = t

        merged = sorted(by_id.values(), key=lambda x: int(x["id"]), reverse=True)
        new_count = sum(1 for t in new_tasks if int(t["id"]) not in existing_ids)
        update_count = len(new_tasks) - new_count

        if not dry_run:
            doc["задачи"] = merged
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")

        written[board_id] = len(new_tasks)
        print(
            f"{path.name}: {len(new_tasks)} задач ({new_count} новых, {update_count} обновлено), "
            f"всего {len(merged)}"
        )

    return written


def fetch_and_merge(
    data_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    meta = load_meta_info(data_dir)
    after_id = int(meta["last_id"])

    projects = load_projects(data_dir)
    if not projects:
        raise SystemExit(f"Нет board-*.json в {data_dir}")

    project_ids = {p.project_id for p in projects}
    board_ids = {p.board_id for p in projects}
    backfill_boards = empty_board_ids(projects)

    print(f"Синхронизация задач с id > {after_id} для {len(projects)} досок…")
    if backfill_boards:
        labels = [
            f"{p.label} (board {p.board_id})"
            for p in projects
            if p.board_id in backfill_boards
        ]
        print(f"Пустые доски — полная догрузка: {', '.join(labels)}")
    candidate_ids = collect_candidate_ids(
        project_ids,
        board_ids,
        after_id=after_id,
        backfill_board_ids=backfill_boards,
    )
    print(f"Найдено в API: {len(candidate_ids)} задач")

    if not candidate_ids:
        last_id, last_date = finalize_meta_info(data_dir, dry_run=dry_run)
        if dry_run:
            print(f"dry-run: meta-info не изменён (last_id={last_id}, last_date={last_date})")
        else:
            print(f"meta-info.json: last_id={last_id}, last_date={last_date}")
        n_bak = remove_bak_files(data_dir)
        if n_bak:
            print(f"Удалено .bak: {n_bak}")
        return last_id

    column_cache: dict[int, dict[int, str]] = {}
    exports_by_board: dict[int, list[dict[str, Any]]] = {}

    for i, tid in enumerate(candidate_ids, 1):
        try:
            task = get_task(tid)
        except WeeekApiError as e:
            logger.error("get_task(%s): %s", tid, e)
            continue

        board_id = int(task.get("boardId") or 0)
        if board_id not in board_ids:
            continue

        col_map = _column_map(board_id, column_cache)
        export = api_task_to_export(task, column_names=col_map)
        exports_by_board.setdefault(board_id, []).append(export)

        if i % 10 == 0 or i == len(candidate_ids):
            print(f"  загружено {i}/{len(candidate_ids)}")

    merge_tasks_into_boards(projects, exports_by_board, dry_run=dry_run)

    last_id, last_date = finalize_meta_info(data_dir, dry_run=dry_run)
    if dry_run:
        print(f"dry-run: meta-info не изменён (last_id={last_id}, last_date={last_date})")
    else:
        print(f"meta-info.json: last_id={last_id}, last_date={last_date}")

    n_bak = remove_bak_files(data_dir)
    if n_bak:
        print(f"Удалено .bak: {n_bak}")

    return last_id


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=(
            "Скачать из Weeek задачи с id > meta-info.last_id и дописать в data/board-*.json. "
            "Для досок без задач в JSON — все задачи доски."
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Не записывать файлы")
    args = parser.parse_args()

    try:
        fetch_and_merge(args.data_dir, dry_run=args.dry_run)
    except WeeekApiError as e:
        raise SystemExit(f"Weeek API: {e}") from e


if __name__ == "__main__":
    main()
