# -*- coding: utf-8 -*-
"""Report duplicate parsed Markdown groups with Zotero metadata.

This script is read-only. It does not modify Zotero, parsed/, or ChromaDB.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402


DEFAULT_OUTPUT_DIR = Path(os.environ.get("ZOTERO_REVIEW_DIR", Path.home() / "zotero-llm-wiki"))
DEFAULT_ZOTERO_SQLITE = Path(os.environ.get("ZOTERO_SQLITE", Path.home() / "Zotero" / "zotero.sqlite"))


@dataclass
class PaperMeta:
    key: str
    item_id: int | None = None
    item_type: str = ""
    title: str = ""
    doi: str = ""
    year: str = ""
    publication: str = ""
    creators: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)


def _norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _norm_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return _norm_space(value)


def _norm_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.strip()


def _year_from(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", value or "")
    return match.group(0) if match else ""


def _title_similarity(a: str, b: str) -> float:
    a_norm = _norm_title(a)
    b_norm = _norm_title(b)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _head_title_score(head: str, title: str) -> float:
    head_norm = _norm_title(head)
    tokens = [t for t in _norm_title(title).split() if len(t) >= 4]
    if not head_norm or not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in head_norm)
    return hits / len(tokens)


def _connect_zotero(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Zotero SQLite not found: {path}")
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _chunks(values: list[Any], size: int = 500):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _load_metadata(sqlite_path: Path, keys: list[str]) -> dict[str, PaperMeta]:
    if not keys:
        return {}
    con = _connect_zotero(sqlite_path)
    try:
        found: dict[str, PaperMeta] = {}
        for batch in _chunks(keys):
            placeholders = ",".join("?" for _ in batch)
            rows = con.execute(
                f"""
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
                WHERE i.key IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                found[row["key"]] = PaperMeta(
                    key=row["key"],
                    item_id=row["itemID"],
                    item_type=row["typeName"] or "",
                )

        by_id = {meta.item_id: meta for meta in found.values() if meta.item_id is not None}
        item_ids = list(by_id)
        for batch in _chunks(item_ids):
            placeholders = ",".join("?" for _ in batch)
            for row in con.execute(
                f"""
                SELECT d.itemID, f.fieldName, v.value
                FROM itemData d
                JOIN fields f ON f.fieldID = d.fieldID
                JOIN itemDataValues v ON v.valueID = d.valueID
                WHERE d.itemID IN ({placeholders})
                """,
                batch,
            ):
                meta = by_id[row["itemID"]]
                field = row["fieldName"]
                value = row["value"] or ""
                if field == "title":
                    meta.title = value
                elif field == "DOI":
                    meta.doi = value
                elif field == "date":
                    meta.year = _year_from(value)
                elif field in {"publicationTitle", "proceedingsTitle", "bookTitle"} and not meta.publication:
                    meta.publication = value

            for row in con.execute(
                f"""
                SELECT ic.itemID, c.firstName, c.lastName
                FROM itemCreators ic
                JOIN creators c ON c.creatorID = ic.creatorID
                WHERE ic.itemID IN ({placeholders})
                ORDER BY ic.itemID, ic.orderIndex
                """,
                batch,
            ):
                first = (row["firstName"] or "").strip()
                last = (row["lastName"] or "").strip()
                name = " ".join(x for x in [first, last] if x).strip()
                if name:
                    by_id[row["itemID"]].creators.append(name)

            for row in con.execute(
                f"""
                SELECT ci.itemID, c.collectionName
                FROM collectionItems ci
                JOIN collections c ON c.collectionID = ci.collectionID
                WHERE ci.itemID IN ({placeholders})
                ORDER BY lower(c.collectionName)
                """,
                batch,
            ):
                name = (row["collectionName"] or "").strip()
                if name:
                    by_id[row["itemID"]].collections.append(name)

        return found
    finally:
        con.close()


def _latest_audit_json(output_dir: Path) -> Path:
    audits = output_dir / "audits"
    files = sorted(audits.glob("ingest-audit-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"No ingest audit JSON found under: {audits}")
    return files[0]


def _classify_group(items: list[dict[str, Any]], metadata: dict[str, PaperMeta]) -> str:
    metas = [metadata.get(item["key"], PaperMeta(key=item["key"])) for item in items]
    missing_meta = [
        meta for meta in metas
        if meta.item_id is None or (not meta.title and not meta.doi)
    ]
    if missing_meta:
        return "needs_zotero_key_check"
    dois = {_norm_doi(meta.doi) for meta in metas if _norm_doi(meta.doi)}
    titles = [_norm_title(meta.title) for meta in metas if _norm_title(meta.title)]
    distinct_titles = set(titles)
    head = items[0].get("head", "")
    head_scores = [_head_title_score(head, meta.title) for meta in metas if meta.title]

    if len(dois) == 1 and len(distinct_titles) <= 1:
        return "likely_duplicate_same_doi_title"
    if len(dois) == 1:
        return "likely_duplicate_same_doi"
    if titles and len(distinct_titles) == 1:
        return "likely_duplicate_same_title"
    if len(titles) > 1:
        base = metas[0].title
        if base and all(_title_similarity(base, meta.title) >= 0.9 for meta in metas[1:] if meta.title):
            return "likely_duplicate_near_title"
    if head_scores and max(head_scores) < 0.35:
        return "suspicious_head_metadata_mismatch"
    return "suspicious_metadata_mismatch"


def _recommended_action(classification: str) -> str:
    if classification == "needs_zotero_key_check":
        return "核对这些 parsed key 是否仍是 Zotero 父条目；不要直接删除，先确认是否为历史孤儿缓存或附件 key。"
    if classification.startswith("likely_duplicate"):
        return "优先在 Zotero 合并重复父条目；parsed 缓存可暂时保留，避免重新解析。"
    if classification.startswith("suspicious"):
        return "优先检查 head_title_score 低的 key；疑似错配时移走该 key 的 parsed 缓存并单独重解析。"
    return "人工检查。"


def _write_reports(groups: list[dict[str, Any]], metadata: dict[str, PaperMeta], output_dir: Path) -> dict[str, str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    audit_dir = output_dir / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    json_path = audit_dir / f"parsed-duplicate-report-{stamp}.json"
    md_path = audit_dir / f"parsed-duplicate-report-{stamp}.md"
    csv_path = audit_dir / f"parsed-duplicate-report-{stamp}.csv"

    enriched = []
    rows = []
    class_counts: dict[str, int] = {}
    for group in groups:
        classification = _classify_group(group["items"], metadata)
        action = _recommended_action(classification)
        class_counts[classification] = class_counts.get(classification, 0) + 1
        enriched_items = []
        for item in group["items"]:
            meta = metadata.get(item["key"], PaperMeta(key=item["key"]))
            score = _head_title_score(item.get("head", ""), meta.title)
            enriched_item = {
                **item,
                "title": meta.title,
                "doi": meta.doi,
                "year": meta.year,
                "publication": meta.publication,
                "creators": meta.creators[:5],
                "collections": meta.collections,
                "head_title_score": round(score, 3),
            }
            enriched_items.append(enriched_item)
            rows.append({
                "classification": classification,
                "hash": group["hash"],
                "group_count": group["count"],
                "key": item["key"],
                "recommended_action": action,
                "title": meta.title,
                "doi": meta.doi,
                "year": meta.year,
                "publication": meta.publication,
                "collections": "; ".join(meta.collections),
                "head_title_score": round(score, 3),
                "path": item.get("path", ""),
            })
        enriched.append({
            "hash": group["hash"],
            "count": group["count"],
            "classification": classification,
            "recommended_action": action,
            "head": group["items"][0].get("head", ""),
            "items": enriched_items,
        })

    json_path.write_text(
        json.dumps(
            {
                "created": datetime.now().isoformat(timespec="seconds"),
                "group_count": len(enriched),
                "classification_counts": class_counts,
                "groups": enriched,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["classification"])
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        f"# Parsed 重复 Markdown 报告 {stamp}",
        "",
        f"- 重复组数：{len(enriched)}",
        f"- 涉及条目数：{sum(group['count'] for group in enriched)}",
        "",
        "## 分类统计",
        "",
    ]
    for name, count in sorted(class_counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"- `{name}`：{count}")
    lines.extend(["", "## 前 30 个重复组", ""])
    for group in enriched[:30]:
        lines.append(f"### {group['hash'][:12]}... ({group['count']} keys; `{group['classification']}`)")
        lines.append("")
        lines.append(f"- 建议动作：{group['recommended_action']}")
        lines.append(f"- Parsed 头部：{group['head'][:240]}")
        for item in group["items"]:
            creators = ", ".join(item["creators"][:3])
            collections = ", ".join(item["collections"][:5]) or "无"
            lines.append(
                f"- `{item['key']}` score={item['head_title_score']} | "
                f"{item['title'] or '(无题名)'} ({item['year'] or 'n.d.'}) | "
                f"DOI: `{item['doi'] or '无'}` | {creators or '无作者'} | Collections: {collections}"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {"json": str(json_path), "markdown": str(md_path), "csv": str(csv_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", type=Path, default=None)
    parser.add_argument("--zotero-sqlite", type=Path, default=DEFAULT_ZOTERO_SQLITE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    audit_json = args.audit_json or _latest_audit_json(args.output_dir)
    audit = json.loads(audit_json.read_text(encoding="utf-8"))
    groups = audit.get("parsed_duplicate_hashes") or []
    keys = sorted({item["key"] for group in groups for item in group.get("items", [])})
    metadata = _load_metadata(args.zotero_sqlite, keys)
    outputs = _write_reports(groups, metadata, args.output_dir)
    print(json.dumps({
        "status": "ok",
        "groups": len(groups),
        "keys": len(keys),
        **outputs,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
