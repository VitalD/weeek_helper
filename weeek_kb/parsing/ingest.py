from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tiktoken

from weeek_kb.config import DATA_DIR, OPENAI_EMBED_MODEL
from weeek_kb.parsing.html_utils import format_comments, strip_html
from weeek_kb.projects import Project, collection_name_from_stem, load_projects
from weeek_kb.search.vector_store import create_collection

# OpenAI embedding API: max 8192 tokens per input
_EMBED_MAX_TOKENS = 8000
_UPSERT_BATCH = 16
_tiktoken_enc: tiktoken.Encoding | None = None


def _embedding_encoder() -> tiktoken.Encoding:
    global _tiktoken_enc
    if _tiktoken_enc is None:
        try:
            _tiktoken_enc = tiktoken.encoding_for_model(OPENAI_EMBED_MODEL)
        except KeyError:
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_enc


def content_hash(document: str) -> str:
    """SHA256 (first 16 hex) of normalized document text for change detection."""
    normalized = re.sub(r"\s+", " ", document.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def chunk_id(task_id: int, chunk_index: int) -> str:
    return f"task:{task_id}:{chunk_index}"


def truncate_for_embedding(text: str, max_tokens: int = _EMBED_MAX_TOKENS) -> tuple[str, bool]:
    """Clip text so OpenAI embeddings do not exceed the API token limit."""
    enc = _embedding_encoder()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text, False
    return enc.decode(tokens[:max_tokens]), True


def build_document_text(task: dict, meta: dict) -> str:
    title = task.get("название") or ""
    desc = strip_html(task.get("описание") or "")
    comments = format_comments(task.get("comments"))
    parts = [
        f"Название: {title}",
        f"Описание:\n{desc}" if desc else "Описание:",
    ]
    if comments:
        parts.append(f"Комментарии:\n{comments}")
    label = meta.get("метка")
    if label:
        parts.append(f"Проект/метка: {label}")
    return "\n\n".join(parts)


def _chunk_header_lines(title: str, label: str | None, part: int, total: int) -> str:
    lines = [f"Название: {title}"]
    if label:
        lines.append(f"Проект/метка: {label}")
    if total > 1:
        lines.append(f"Фрагмент {part}/{total}")
    return "\n".join(lines) + "\n\n"


def chunk_task_for_embedding(
    task: dict,
    meta: dict,
    max_tokens: int = _EMBED_MAX_TOKENS,
) -> list[str]:
    """
    One embedding document per task if it fits; otherwise several chunks, each ≤ max_tokens,
    with the task title (and label) repeated in every chunk and description/comments split by tokens.
    """
    enc = _embedding_encoder()
    full = build_document_text(task, meta)
    if len(enc.encode(full)) <= max_tokens:
        return [full]

    title = task.get("название") or ""
    desc = strip_html(task.get("описание") or "")
    comments = format_comments(task.get("comments"))
    label = (meta.get("метка") or "").strip() or None

    body_parts: list[str] = []
    if desc:
        body_parts.append(f"Описание:\n{desc}")
    if comments:
        body_parts.append(f"Комментарии:\n{comments}")
    body = "\n\n".join(body_parts) if body_parts else ""

    if not body:
        t, _ = truncate_for_embedding(_chunk_header_lines(title, label, 1, 1).rstrip(), max_tokens)
        return [t]

    # Worst-case header size so body slices never overflow after we choose n.
    worst_header = _chunk_header_lines(title, label, 999, 999)
    overhead = len(enc.encode(worst_header))
    per_body = max(64, max_tokens - overhead - 8)
    body_tokens = enc.encode(body)
    n = max(1, (len(body_tokens) + per_body - 1) // per_body)

    chunks: list[str] = []
    for i in range(n):
        start = i * len(body_tokens) // n
        end = (i + 1) * len(body_tokens) // n
        slice_tokens = body_tokens[start:end]
        header = _chunk_header_lines(title, label, i + 1, n)
        piece = header + enc.decode(slice_tokens)
        if len(enc.encode(piece)) > max_tokens:
            room = max_tokens - len(enc.encode(header))
            if room < 1:
                piece = header
            else:
                piece = header + enc.decode(slice_tokens[:room])
        piece, _ = truncate_for_embedding(piece, max_tokens)
        chunks.append(piece)

    return chunks


def task_metadata(task: dict, meta: dict) -> dict:
    """Chroma: metadata values must be str / int / float / bool — use str for ids in filters."""
    created = task.get("датаСоздания") or ""
    completed = task.get("датаЗавершения") or ""
    if completed is None:
        completed = ""
    return {
        "task_id": str(int(task["id"])),
        "title": (task.get("название") or "")[:2000],
        "status": (task.get("статус") or "")[:200],
        "column": (task.get("колонка") or "")[:200],
        "created": str(created)[:64],
        "completed": str(completed)[:64],
        "project_id": str(int(meta.get("projectId") or 0)),
        "board_id": str(int(meta.get("boardId") or 0)),
        "label": str(meta.get("метка") or "")[:500],
        "chunk_id_version": "v2",
    }


@dataclass
class IngestPlan:
    to_add: list[tuple[str, str, dict]] = field(default_factory=list)
    to_update: list[tuple[str, str, dict]] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)
    skipped: int = 0

    def summary(self) -> str:
        return (
            f"add={len(self.to_add)} update={len(self.to_update)} "
            f"delete={len(self.to_delete)} skip={self.skipped}"
        )


def build_chunks(project: Project) -> tuple[list[tuple[str, str, dict]], int]:
    """
    Read board JSON and return (id, document, metadata) rows plus count of multi-chunk tasks.
    """
    with open(project.file_path, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("meta") or {}
    tasks = data.get("задачи") or []

    rows: list[tuple[str, str, dict]] = []
    chunked_tasks = 0

    for task in tasks:
        tid = int(task["id"])
        task_chunks = chunk_task_for_embedding(task, meta)
        if len(task_chunks) > 1:
            chunked_tasks += 1
        for ci, doc in enumerate(task_chunks):
            cid = chunk_id(tid, ci)
            m = task_metadata(task, meta)
            m["chunk_index"] = str(ci)
            m["chunk_total"] = str(len(task_chunks))
            m["content_hash"] = content_hash(doc)
            rows.append((cid, doc, m))

    return rows, chunked_tasks


def plan_ingest(project: Project, collection: Any) -> IngestPlan:
    desired_rows, _ = build_chunks(project)
    desired: dict[str, tuple[str, dict]] = {cid: (doc, meta) for cid, doc, meta in desired_rows}

    existing_raw = collection.get(include=["metadatas"])
    existing_ids: list[str] = list(existing_raw.get("ids") or [])
    existing_metas: list[dict | None] = list(existing_raw.get("metadatas") or [])

    existing_hash: dict[str, str | None] = {}
    for eid, emeta in zip(existing_ids, existing_metas):
        h = None
        if emeta and isinstance(emeta, dict):
            h = emeta.get("content_hash")
            if h is not None:
                h = str(h)
        existing_hash[eid] = h

    plan = IngestPlan()
    for cid, (doc, meta) in desired.items():
        stored = existing_hash.get(cid)
        if stored is None:
            plan.to_add.append((cid, doc, meta))
        elif stored == meta["content_hash"]:
            plan.skipped += 1
        else:
            plan.to_update.append((cid, doc, meta))

    desired_ids = set(desired)
    plan.to_delete = [eid for eid in existing_ids if eid not in desired_ids]
    return plan


def _build_plan_full_reindex(project: Project) -> IngestPlan:
    rows, _ = build_chunks(project)
    plan = IngestPlan()
    plan.to_add = list(rows)
    return plan


def apply_plan(collection: Any, plan: IngestPlan) -> None:
    upsert_rows = plan.to_add + plan.to_update
    if upsert_rows:
        ids = [r[0] for r in upsert_rows]
        documents = [r[1] for r in upsert_rows]
        metadatas = [r[2] for r in upsert_rows]
        for i in range(0, len(ids), _UPSERT_BATCH):
            collection.upsert(
                ids=ids[i : i + _UPSERT_BATCH],
                documents=documents[i : i + _UPSERT_BATCH],
                metadatas=metadatas[i : i + _UPSERT_BATCH],
            )
    if plan.to_delete:
        collection.delete(ids=plan.to_delete)


def ingest_file(project: Project, reset: bool, dry_run: bool = False) -> IngestPlan:
    col = create_collection(project.collection_name, reset=reset)

    _, chunked_tasks = build_chunks(project)
    if chunked_tasks:
        print(f"  note: {chunked_tasks} task(s) split into multiple chunks (<={_EMBED_MAX_TOKENS} tokens each)")

    if reset:
        plan = _build_plan_full_reindex(project)
    else:
        plan = plan_ingest(project, col)

    if not dry_run:
        apply_plan(col, plan)

    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Weeek JSON boards into ChromaDB")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Directory with board-*.json",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Only this collection stem (e.g. board-6-avrora-kanc-rf or akord-kazan-ru)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate collections (full reindex)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute ingest plan without writing to Chroma",
    )
    args = parser.parse_args()

    projects = load_projects(args.data_dir)
    if args.only:
        stem = args.only.replace(".json", "")
        projects = [p for p in projects if p.file_stem == stem or p.collection_name == collection_name_from_stem(stem)]
        if not projects:
            raise SystemExit(f"No project matches --only {args.only!r}")

    totals = {"add": 0, "update": 0, "delete": 0, "skip": 0}
    for p in projects:
        plan = ingest_file(p, reset=args.reset, dry_run=args.dry_run)
        print(f"{p.file_path.name} -> collection={p.collection_name}  {plan.summary()}")
        totals["add"] += len(plan.to_add)
        totals["update"] += len(plan.to_update)
        totals["delete"] += len(plan.to_delete)
        totals["skip"] += plan.skipped

    print(
        f"Done. add={totals['add']} update={totals['update']} "
        f"delete={totals['delete']} skip={totals['skip']}"
    )


if __name__ == "__main__":
    main()
