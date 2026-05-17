from __future__ import annotations

import re
from typing import Any

CREDENTIALS_REFUSAL = (
    "Я не отвечаю на вопросы про логины, пароли, доступы, токены и другие учётные данные. "
    "Спросите о содержании работ по задачам (статус, что сделано, описание без секретов)."
)

_RE_FLAGS = re.IGNORECASE | re.UNICODE

# Подстроки в строке задачи (после casefold)
_LINE_MARKERS: tuple[str, ...] = (
    "password:",
    "password=",
    "passwd:",
    "passwd=",
    "pwd:",
    "pwd=",
    "login:",
    "login=",
    "username:",
    "user:",
    "secret:",
    "secret=",
    "token:",
    "token=",
    "api key:",
    "api-key:",
    "apikey:",
    "access key:",
    "ftp://",
    "sftp://",
    "ssh://",
    # кириллица
    "пароль:",
    "пароль=",
    "парол:",
    "логин:",
    "логин=",
    "учетные данные",
    "учётные данные",
)

# Запрос пользователя (после casefold): маркеры и глаголы
_QUERY_MARKERS: tuple[str, ...] = _LINE_MARKERS + (
    "логин и парол",
    "парол и логин",
    "данные для входа",
    "доступ к ftp",
    "доступ к ssh",
    "доступ к админ",
    "доступ к сайт",
    "доступ к сервер",
    "доступ к хостинг",
    "доступ к панел",
    "доступ к баз",
)

_QUERY_VERB_PATTERN = re.compile(
    r"(?:какой|какая|какие|где|дай|скинь|подскажи|нужен|нужна|нужны|пришл|отправ|"
    r"напиши|сообщи|вспомни|знаешь|есть\s+ли).{0,60}"
    r"(?:парол|логин|password|login|доступ|учетн|учётн|credential|api\s*key|токен|secret|ftp|ssh)",
    _RE_FLAGS,
)

_QUERY_TOPIC_PATTERN = re.compile(
    r"(?:парол|логин|password|login|passwd|credential|api\s*key|токен|secret)"
    r".{0,50}(?:от|к|для|на|сайт|ftp|ssh|баз|admin|хостинг|панел|сервер|почт)",
    _RE_FLAGS,
)

_REDACT_PLACEHOLDER = "[учётные данные скрыты]"


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(m in folded for m in markers)


def is_credentials_related_query(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 3:
        return False
    folded = t.casefold()
    if _contains_marker(folded, _QUERY_MARKERS):
        return True
    if _QUERY_VERB_PATTERN.search(folded):
        return True
    if _QUERY_TOPIC_PATTERN.search(folded):
        return True
    if re.match(r"^\s*(?:парол|логин|password|login|ftp|ssh)\b", folded):
        return True
    return False


def _redact_line(line: str) -> str:
    if not line.strip():
        return line
    if _contains_marker(line, _LINE_MARKERS):
        return _REDACT_PLACEHOLDER
    return line


def sanitize_document_text(text: str) -> str:
    """Убрать из текста задачи строки с паролями и доступами."""
    if not (text or "").strip():
        return text or ""
    lines = text.split("\n")
    cleaned = [_redact_line(ln) for ln in lines]
    out: list[str] = []
    for ln in cleaned:
        if ln == _REDACT_PLACEHOLDER and out and out[-1] == _REDACT_PLACEHOLDER:
            continue
        out.append(ln)
    return "\n".join(out)


def document_is_primarily_sensitive(text: str) -> bool:
    """Исключить задачу целиком, если после очистки почти не осталось содержания."""
    raw = (text or "").strip()
    if not raw:
        return False
    sanitized = sanitize_document_text(raw)
    if sanitized.strip() == _REDACT_PLACEHOLDER:
        return True
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    if not lines:
        return False
    hidden = sum(1 for ln in lines if _redact_line(ln) == _REDACT_PLACEHOLDER)
    if hidden / len(lines) >= 0.5:
        return True
    useful = re.sub(re.escape(_REDACT_PLACEHOLDER), "", sanitized, flags=re.IGNORECASE)
    useful = re.sub(r"\s+", " ", useful).strip()
    return len(useful) < 40 and hidden > 0


def filter_merged_hits(
    merged: list[tuple[str, float, dict[str, Any], str]],
) -> list[tuple[str, float, dict[str, Any], str]]:
    """Исключить или очистить задачи с учётными данными в выдаче поиска."""
    out: list[tuple[str, float, dict[str, Any], str]] = []
    for task_id, dist, meta, doc in merged:
        if document_is_primarily_sensitive(doc):
            continue
        clean = sanitize_document_text(doc)
        if not clean.strip():
            continue
        out.append((task_id, dist, meta, clean))
    return out
