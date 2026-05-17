"""Удаление HTML из полей описание и комментарий в data/board-*.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from weeek_kb.config import DATA_DIR
from weeek_kb.parsing.html_utils import sanitize_task_record


def clean_task(task: dict) -> tuple[bool, bool]:
    """Возвращает (changed_описание, changed_комментарии)."""
    desc_before = task.get("описание")
    comments_before = None
    if isinstance(task.get("comments"), list):
        comments_before = [
            c.get("комментарий") if isinstance(c, dict) else None for c in task["comments"]
        ]

    sanitize_task_record(task)

    changed_desc = desc_before != task.get("описание")
    changed_comments = False
    if comments_before is not None and isinstance(task.get("comments"), list):
        for before, c in zip(comments_before, task["comments"], strict=False):
            if isinstance(c, dict) and before != c.get("комментарий"):
                changed_comments = True

    return changed_desc, changed_comments


def clean_file(path: Path, *, dry_run: bool) -> dict[str, int]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    tasks = data.get("задачи") or []
    stats = {"tasks": len(tasks), "desc": 0, "comments": 0}

    for task in tasks:
        if not isinstance(task, dict):
            continue
        d, c = clean_task(task)
        if d:
            stats["desc"] += 1
        if c:
            stats["comments"] += 1

    if not dry_run and (stats["desc"] or stats["comments"]):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Strip HTML from описание and комментарий in board JSON")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Only report, do not write files")
    args = parser.parse_args()

    paths = sorted(args.data_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"No JSON files in {args.data_dir}")

    total_desc = total_comments = 0
    for path in paths:
        stats = clean_file(path, dry_run=args.dry_run)
        total_desc += stats["desc"]
        total_comments += stats["comments"]
        flag = ""
        if stats["desc"] or stats["comments"]:
            flag = f" — описаний: {stats['desc']}, задач с комментариями: {stats['comments']}"
        print(f"{path.name}: {stats['tasks']} задач{flag}")

    mode = "dry-run" if args.dry_run else "записано"
    print(f"Готово ({mode}). Всего очищено описаний: {total_desc}, блоков комментариев: {total_comments}.")


if __name__ == "__main__":
    main()
