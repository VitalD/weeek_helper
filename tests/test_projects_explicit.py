from __future__ import annotations

from pathlib import Path

from weeek_kb.projects import Project, explicit_project_from_text, projects_explicitly_mentioned


def _projects() -> list[Project]:
    return [
        Project(
            file_path=Path("board-10-avrorastore-ru.json"),
            collection_name="board-10-avrorastore-ru",
            label="avrorastore.ru",
            project_id=1,
            board_id=10,
        ),
        Project(
            file_path=Path("board-24-makukhinmoscow-com.json"),
            collection_name="board-24-makukhinmoscow-com",
            label="makukhinmoscow.com",
            project_id=2,
            board_id=24,
        ),
    ]


def test_summer_sale_without_site_is_not_explicit() -> None:
    q = "Как дела с проведением акции Summer Sale?"
    assert explicit_project_from_text(q, _projects()) is None
    assert len(projects_explicitly_mentioned(q, _projects())) == 0


def test_domain_in_text_is_explicit() -> None:
    q = "Как дела с акцией на avrorastore.ru?"
    p = explicit_project_from_text(q, _projects())
    assert p is not None
    assert p.board_id == 10


def test_makukhin_slug_in_text() -> None:
    q = "Статус задач по makukhinmoscow.com"
    p = explicit_project_from_text(q, _projects())
    assert p is not None
    assert p.board_id == 24


def test_riva_import_task_without_site_is_not_explicit() -> None:
    q = (
        "Поставь задачу: реализовать логи импорта товаров Riva, "
        "чтобы клиент видел, сколько товаров загрузилось"
    )
    assert explicit_project_from_text(q, _projects()) is None
