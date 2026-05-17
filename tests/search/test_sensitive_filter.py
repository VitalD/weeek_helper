from __future__ import annotations

from weeek_kb.search.sensitive_filter import (
    CREDENTIALS_REFUSAL,
    document_is_primarily_sensitive,
    filter_merged_hits,
    is_credentials_related_query,
    sanitize_document_text,
)


def test_query_blocked() -> None:
    assert is_credentials_related_query("Какой пароль от ftp сайта?")
    assert is_credentials_related_query("дай логин и пароль к админке")
    assert is_credentials_related_query("нужен доступ к ssh серверу")


def test_query_allowed() -> None:
    assert not is_credentials_related_query("Какие задачи по доставке?")
    assert not is_credentials_related_query("Статус задачи про каталог")


def test_sanitize_line() -> None:
    raw = "Описание работы\nПароль: Secret123\nГотово"
    clean = sanitize_document_text(raw)
    assert "Secret123" not in clean
    assert "учётные данные скрыты" in clean
    assert "Описание работы" in clean


def test_filter_drops_sensitive_task() -> None:
    merged = [
        (
            "1",
            0.1,
            {"title": "FTP"},
            "логин: admin\nпароль: qwerty\nпароль: test2",
        ),
        ("2", 0.2, {"title": "OK"}, "Сделали выгрузку каталога"),
    ]
    out = filter_merged_hits(merged)
    assert len(out) == 1
    assert out[0][0] == "2"


def test_refusal_message_non_empty() -> None:
    assert "парол" in CREDENTIALS_REFUSAL.lower()


def test_primarily_sensitive() -> None:
    assert document_is_primarily_sensitive("password: x\nlogin: y")
