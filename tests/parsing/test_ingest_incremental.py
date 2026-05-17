from __future__ import annotations

import hashlib
import json
from pathlib import Path
import chromadb
import pytest
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from weeek_kb.parsing.ingest import (
    IngestPlan,
    _build_plan_full_reindex,
    apply_plan,
    plan_ingest,
)
from weeek_kb.projects import Project, collection_name_from_stem


class FakeEmbeddingFunction(EmbeddingFunction[Documents]):
    """Deterministic 16-dim vectors from SHA256 — no OpenAI calls."""

    def __call__(self, input: Documents) -> Embeddings:
        out: Embeddings = []
        for text in input:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([float(b) / 255.0 for b in digest[:16]])
        return out


def _make_test_collection(name: str) -> Collection:
    client = chromadb.EphemeralClient()
    return client.get_or_create_collection(
        name=name,
        embedding_function=FakeEmbeddingFunction(),
    )


def _task(
    tid: int,
    title: str,
    desc: str = "<p>desc</p>",
    comments: list[dict] | None = None,
) -> dict:
    return {
        "id": tid,
        "название": title,
        "описание": desc,
        "статус": "Активна",
        "колонка": "TODO",
        "датаСоздания": "2026-01-01",
        "датаЗавершения": "",
        "comments": comments or [],
    }


SAMPLE_JSON: dict = {
    "meta": {"projectId": 1, "boardId": 1, "метка": "test-board"},
    "задачи": [
        _task(1, "T1"),
        _task(2, "T2"),
        _task(3, "T3"),
    ],
}


def _write_board(tmp_path: Path, data: dict | None = None) -> Project:
    path = tmp_path / "board-99-test-board.json"
    path.write_text(json.dumps(data or SAMPLE_JSON, ensure_ascii=False), encoding="utf-8")
    stem = path.stem
    return Project(
        file_path=path,
        collection_name=collection_name_from_stem(stem),
        label="test-board",
        project_id=1,
        board_id=1,
    )


def _apply(project: Project, col: Collection) -> IngestPlan:
    plan = plan_ingest(project, col)
    apply_plan(col, plan)
    return plan


def _count(col: Collection) -> int:
    return col.count()


@pytest.fixture
def board_project(tmp_path: Path) -> Project:
    return _write_board(tmp_path)


@pytest.fixture
def collection() -> Collection:
    return _make_test_collection("test-ingest")


def test_first_run_creates_all(board_project: Project, collection: Collection) -> None:
    plan = plan_ingest(board_project, collection)
    assert len(plan.to_add) == 3
    assert len(plan.to_update) == 0
    assert len(plan.to_delete) == 0
    assert plan.skipped == 0

    apply_plan(collection, plan)
    assert _count(collection) == 3


def test_second_run_is_noop(board_project: Project, collection: Collection) -> None:
    _apply(board_project, collection)
    plan = plan_ingest(board_project, collection)
    assert len(plan.to_add) == 0
    assert len(plan.to_update) == 0
    assert len(plan.to_delete) == 0
    assert plan.skipped == 3
    assert _count(collection) == 3


def test_edit_one_task(board_project: Project, collection: Collection, tmp_path: Path) -> None:
    _apply(board_project, collection)
    data = json.loads(board_project.file_path.read_text(encoding="utf-8"))
    data["задачи"][0]["описание"] = "<p>updated description</p>"
    project = _write_board(tmp_path, data)

    plan = plan_ingest(project, collection)
    assert len(plan.to_add) == 0
    assert len(plan.to_update) == 1
    assert len(plan.to_delete) == 0
    assert plan.skipped == 2


def test_delete_one_task(board_project: Project, collection: Collection, tmp_path: Path) -> None:
    _apply(board_project, collection)
    data = json.loads(board_project.file_path.read_text(encoding="utf-8"))
    data["задачи"] = data["задачи"][:2]
    project = _write_board(tmp_path, data)

    plan = plan_ingest(project, collection)
    assert len(plan.to_add) == 0
    assert len(plan.to_update) == 0
    assert len(plan.to_delete) == 1
    assert plan.skipped == 2

    apply_plan(collection, plan)
    assert _count(collection) == 2


def test_add_one_task(board_project: Project, collection: Collection, tmp_path: Path) -> None:
    _apply(board_project, collection)
    data = json.loads(board_project.file_path.read_text(encoding="utf-8"))
    data["задачи"].append(_task(4, "T4"))
    project = _write_board(tmp_path, data)

    plan = plan_ingest(project, collection)
    assert len(plan.to_add) == 1
    assert len(plan.to_update) == 0
    assert len(plan.to_delete) == 0
    assert plan.skipped == 3


def test_add_comment(board_project: Project, collection: Collection, tmp_path: Path) -> None:
    _apply(board_project, collection)
    data = json.loads(board_project.file_path.read_text(encoding="utf-8"))
    data["задачи"][0]["comments"] = [
        {"автор": "User", "дата": "2026-05-01", "комментарий": "<p>New comment</p>"}
    ]
    project = _write_board(tmp_path, data)

    plan = plan_ingest(project, collection)
    assert len(plan.to_add) == 0
    assert len(plan.to_update) == 1
    assert len(plan.to_delete) == 0
    assert plan.skipped == 2


def test_reset_reindex_count(board_project: Project, collection: Collection) -> None:
    plan = _build_plan_full_reindex(board_project)
    assert len(plan.to_add) == 3
    apply_plan(collection, plan)
    assert _count(collection) == 3


def test_content_hash_stable() -> None:
    from weeek_kb.parsing.ingest import content_hash

    assert content_hash("a  b\nc") == content_hash("a b c")
    assert content_hash("  hello  ") == content_hash("hello")
