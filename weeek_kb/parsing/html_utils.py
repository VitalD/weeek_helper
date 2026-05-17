from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_task_record(task: dict) -> None:
    """Удаляет HTML из полей «описание» и «комментарий» в записи задачи (in-place)."""
    if "описание" in task and task["описание"]:
        task["описание"] = strip_html(str(task["описание"]))

    comments = task.get("comments")
    if isinstance(comments, list):
        for c in comments:
            if not isinstance(c, dict):
                continue
            raw = c.get("комментарий")
            if raw is None or raw == "":
                continue
            c["комментарий"] = strip_html(str(raw))


def format_comments(comments: list[dict] | None) -> str:
    if not comments:
        return ""
    parts: list[str] = []
    for c in comments:
        author = c.get("автор") or ""
        date = c.get("дата") or ""
        body = strip_html(c.get("комментарий") or "")
        if not body:
            continue
        head = f"{author} ({date})".strip()
        if head.startswith("("):
            head = date
        parts.append(f"- {head}: {body}" if head else f"- {body}")
    return "\n".join(parts)
