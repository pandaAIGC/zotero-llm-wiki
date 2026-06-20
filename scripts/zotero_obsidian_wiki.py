"""Build an Obsidian wiki layer from the local Zotero Brain library.

The script is read-only toward Zotero, parsed/, and ChromaDB. It writes
managed Markdown pages into an Obsidian wiki folder while preserving manual
notes outside generated blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chromadb
from chromadb.config import Settings

import config


DEFAULT_ZOTERO_SQLITE = Path(os.environ.get("ZOTERO_SQLITE", Path.home() / "Zotero" / "zotero.sqlite"))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("ZOTERO_LLM_WIKI_DIR", Path.home() / "zotero-llm-wiki" / "wiki"))
DEFAULT_VAULT_LINK_PREFIX = os.environ.get("ZOTERO_LLM_WIKI_LINK_PREFIX", "zotero-llm-wiki/wiki").strip("/")
DEFAULT_SCHEMA_TEMPLATE = ROOT / "templates" / "wiki" / "AGENTS.md"
DEFAULT_LIMIT = 50
VAULT_LINK_PREFIX = DEFAULT_VAULT_LINK_PREFIX
BEGIN = "<!-- ZOTERO_BRAIN_WIKI:BEGIN -->"
END = "<!-- ZOTERO_BRAIN_WIKI:END -->"
MANAGED_BY = "zotero-brain-wiki"
EXCLUDED_TYPES = {"attachment", "note", "annotation"}


@dataclass
class Paper:
    item_id: int
    key: str
    item_type: str
    date_added: str
    date_modified: str
    fields: dict[str, str] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    collection_keys: list[str] = field(default_factory=list)
    has_pdf: bool = False
    parsed: bool = False
    chroma_collection: str = ""
    chroma_safe_collection: str = ""
    chroma_chunks: int = 0
    chunk_previews: list[dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.fields.get("title", "").strip() or f"Untitled {self.key}"

    @property
    def abstract(self) -> str:
        return self.fields.get("abstractNote", "").strip()

    @property
    def doi(self) -> str:
        return self.fields.get("DOI", "").strip()

    @property
    def url(self) -> str:
        return self.fields.get("url", "").strip()

    @property
    def publication(self) -> str:
        return (
            self.fields.get("publicationTitle", "")
            or self.fields.get("proceedingsTitle", "")
            or self.fields.get("bookTitle", "")
        ).strip()

    @property
    def year(self) -> str:
        text = self.fields.get("date", "") or self.date_added
        match = re.search(r"(19|20)\d{2}", text)
        return match.group(0) if match else ""


def _chunks(values: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _connect_zotero(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Zotero SQLite not found: {path}")
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_papers(sqlite_path: Path) -> list[Paper]:
    con = _connect_zotero(sqlite_path)
    try:
        rows = con.execute(
            """
            SELECT i.itemID, i.key, it.typeName, i.dateAdded, i.dateModified
            FROM items i
            JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
            WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
            ORDER BY i.dateModified DESC
            """
        ).fetchall()
        papers = [
            Paper(
                item_id=row["itemID"],
                key=row["key"],
                item_type=row["typeName"],
                date_added=row["dateAdded"],
                date_modified=row["dateModified"],
            )
            for row in rows
            if row["typeName"] not in EXCLUDED_TYPES
        ]
        by_id = {p.item_id: p for p in papers}
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
                by_id[row["itemID"]].fields[row["fieldName"]] = row["value"] or ""

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
                    by_id[row["itemID"]].authors.append(name)

            for row in con.execute(
                f"""
                SELECT it.itemID, t.name
                FROM itemTags it
                JOIN tags t ON t.tagID = it.tagID
                WHERE it.itemID IN ({placeholders})
                ORDER BY lower(t.name)
                """,
                batch,
            ):
                tag = (row["name"] or "").strip()
                if tag:
                    by_id[row["itemID"]].tags.append(tag)

            for row in con.execute(
                f"""
                SELECT ci.itemID, c.collectionName, c.key
                FROM collectionItems ci
                JOIN collections c ON c.collectionID = ci.collectionID
                WHERE ci.itemID IN ({placeholders})
                ORDER BY lower(c.collectionName)
                """,
                batch,
            ):
                paper = by_id[row["itemID"]]
                paper.collections.append(row["collectionName"])
                paper.collection_keys.append(row["key"])

            for row in con.execute(
                f"""
                SELECT parentItemID, contentType, path
                FROM itemAttachments
                WHERE parentItemID IN ({placeholders})
                """,
                batch,
            ):
                content_type = (row["contentType"] or "").lower()
                path = (row["path"] or "").lower()
                if "pdf" in content_type or path.endswith(".pdf"):
                    by_id[row["parentItemID"]].has_pdf = True
    finally:
        con.close()

    return papers


def _parsed_keys() -> set[str]:
    if not config.PARSED_DIR.exists():
        return set()
    return {
        p.name
        for p in config.PARSED_DIR.iterdir()
        if p.is_dir() and any(child.suffix.lower() == ".md" for child in p.iterdir())
    }


def _chroma_client() -> chromadb.ClientAPI:
    return chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def _chroma_summary() -> dict[str, Any]:
    try:
        client = _chroma_client()
        collections = client.list_collections()
        counts = []
        for col in collections:
            try:
                counts.append(
                    {
                        "safe_name": col.name,
                        "name": config.get_display_name(col.name),
                        "count": col.count(),
                    }
                )
            except Exception:
                counts.append({"safe_name": col.name, "name": col.name, "count": 0})
        return {
            "collection_count": len(counts),
            "chunk_count": sum(c["count"] for c in counts),
            "collections": sorted(counts, key=lambda x: x["count"], reverse=True),
        }
    except Exception as exc:
        return {"collection_count": 0, "chunk_count": 0, "collections": [], "error": str(exc)}


def _attach_chroma_state(papers: list[Paper], chunk_preview_limit: int) -> None:
    if not papers:
        return
    keys = [p.key for p in papers]
    by_key = {p.key: p for p in papers}
    try:
        client = _chroma_client()
        for col in client.list_collections():
            for batch in _chunks(keys, 100):
                try:
                    result = col.get(
                        where={"key": {"$in": batch}},
                        include=["metadatas"],
                    )
                except Exception:
                    continue
                for meta in result.get("metadatas") or []:
                    key = meta.get("key") if meta else None
                    if key in by_key:
                        paper = by_key[key]
                        paper.chroma_safe_collection = col.name
                        paper.chroma_collection = config.get_display_name(col.name)
                        paper.chroma_chunks += 1
        if chunk_preview_limit <= 0:
            return
        for paper in papers:
            if not paper.chroma_safe_collection:
                continue
            try:
                col = client.get_collection(paper.chroma_safe_collection)
                result = col.get(
                    where={"key": paper.key},
                    include=["documents", "metadatas"],
                )
            except Exception:
                continue
            paired = []
            for doc, meta in zip(result.get("documents") or [], result.get("metadatas") or []):
                paired.append((int((meta or {}).get("chunk_index", 0)), doc or "", meta or {}))
            paired.sort(key=lambda x: x[0])
            for chunk_index, doc, meta in paired[:chunk_preview_limit]:
                paper.chunk_previews.append(
                    {
                        "chunk_index": chunk_index,
                        "section": meta.get("section", ""),
                        "summary": re.sub(r"\s+", " ", doc).strip()[:240],
                    }
                )
    except Exception as exc:
        print(f"Warning: ChromaDB status unavailable: {exc}", file=sys.stderr)


def _matches_filters(
    paper: Paper,
    query: str,
    collections: list[str],
    tags: list[str],
    parsed_only: bool,
    chroma_only: bool,
) -> bool:
    haystack = " ".join(
        [
            paper.title,
            paper.abstract,
            paper.doi,
            paper.publication,
            " ".join(paper.tags),
            " ".join(paper.collections),
            " ".join(paper.authors),
        ]
    ).lower()
    if query and query.lower() not in haystack:
        return False
    if collections:
        collection_text = "\n".join(paper.collections).lower()
        if not any(c.lower() in collection_text for c in collections):
            return False
    if tags:
        tag_text = "\n".join(paper.tags).lower()
        if not any(t.lower() in tag_text for t in tags):
            return False
    if parsed_only and not paper.parsed:
        return False
    if chroma_only and not paper.chroma_collection:
        return False
    return True


def _slug(text: str, fallback: str = "untitled", max_len: int = 80) -> str:
    text = re.sub(r"[\\/:*?\"<>|#^[\]]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    return text[:max_len].rstrip(" .")


def _paper_filename(paper: Paper) -> str:
    bits = [paper.year, _slug(paper.title, paper.key, 70), paper.key]
    return " - ".join(bit for bit in bits if bit) + ".md"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", " ").replace("\n", " ")
    return f'"{text}"'


def _yaml_list(values: Iterable[str]) -> list[str]:
    values = [v for v in values if v]
    if not values:
        return [" []"]
    return [""] + [f"  - {_yaml_scalar(v)}" for v in values]


def _frontmatter(paper: Paper) -> str:
    lines = [
        "---",
        f"managed_by: {_yaml_scalar(MANAGED_BY)}",
        'type: "zotero-literature-note"',
        f"zotero_key: {_yaml_scalar(paper.key)}",
        f"title: {_yaml_scalar(paper.title)}",
        f"year: {_yaml_scalar(paper.year)}",
        f"item_type: {_yaml_scalar(paper.item_type)}",
        f"publication: {_yaml_scalar(paper.publication)}",
        f"doi: {_yaml_scalar(paper.doi)}",
        f"url: {_yaml_scalar(paper.url)}",
        f"has_pdf: {str(paper.has_pdf).lower()}",
        f"parsed: {str(paper.parsed).lower()}",
        f"chroma_collection: {_yaml_scalar(paper.chroma_collection)}",
        "authors:" + "\n".join(_yaml_list(paper.authors[:12])),
        "collections:" + "\n".join(_yaml_list(paper.collections)),
        "tags:" + "\n".join(_yaml_list(paper.tags[:30])),
        "---",
    ]
    return "\n".join(lines)


def _obsidian_link(path_from_vault: str, label: str | None = None) -> str:
    return f"[[{path_from_vault}|{label or Path(path_from_vault).name}]]"


def _wiki_path(*parts: str) -> str:
    tail = "/".join(part.strip("/") for part in parts if part)
    if VAULT_LINK_PREFIX:
        return f"{VAULT_LINK_PREFIX}/{tail}" if tail else VAULT_LINK_PREFIX
    return tail


def _literature_link_map(papers: list[Paper]) -> dict[str, str]:
    result = {}
    for paper in papers:
        stem = Path(_paper_filename(paper)).stem
        result[paper.key] = _wiki_path("literature", stem)
    return result


def _status_badges(paper: Paper) -> str:
    parts = [
        "PDF：有" if paper.has_pdf else "PDF：无",
        "已解析：是" if paper.parsed else "已解析：否",
        f"向量片段：{paper.chroma_chunks}" if paper.chroma_chunks else "向量片段：无",
    ]
    return " | ".join(parts)


def _paper_managed_body(
    paper: Paper,
    link_map: dict[str, str],
    related_map: dict[str, list[str]],
) -> str:
    collection_links = [
        _obsidian_link(_wiki_path("collections", _slug(c)), c)
        for c in paper.collections
    ]
    tag_links = [
        _obsidian_link(_wiki_path("topics", _slug(t)), t)
        for t in paper.tags[:20]
    ]
    lines = [
        BEGIN,
        f"_生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "## 来源信息",
        "",
        f"- Zotero key: `{paper.key}`",
        f"- Zotero 链接：`zotero://select/library/items/{paper.key}`",
        f"- 状态：{ _status_badges(paper) }",
        f"- DOI：`{paper.doi or '无'}`",
        f"- 期刊/来源：{paper.publication or '无'}",
        f"- 年份：{paper.year or '无'}",
        f"- 作者：{', '.join(paper.authors[:8]) or '无'}",
        f"- Zotero collections：{', '.join(collection_links) if collection_links else '无'}",
        f"- 标签：{', '.join(tag_links) if tag_links else '无'}",
        "",
        "## 原始摘要",
        "",
        paper.abstract or "_Zotero 元数据中暂无摘要。_",
        "",
        "## Zotero Brain 全文证据",
        "",
    ]
    if paper.chunk_previews:
        for chunk in paper.chunk_previews:
            section = f" ({chunk['section']})" if chunk.get("section") else ""
            lines.append(f"- 片段 {chunk['chunk_index']}{section}：{chunk['summary']}")
    elif paper.chroma_chunks:
        lines.append(f"- 已进入 ChromaDB collection `{paper.chroma_collection}`，共 {paper.chroma_chunks} 个向量片段。")
    elif paper.parsed:
        lines.append("- 已有 parsed Markdown，但当前样本暂未在 ChromaDB 中找到向量片段。")
    else:
        lines.append("- 尚未解析或向量化。")

    related = related_map.get(paper.key, [])
    lines.extend(["", "## 相关文献", ""])
    if related:
        lines.extend(f"- {link}" for link in related)
    else:
        lines.append("- _本次导出批次中暂无相关文献。_")
    lines.extend(["", END])
    return "\n".join(lines)


def _related_link_map(papers: list[Paper], link_map: dict[str, str]) -> dict[str, list[str]]:
    related: dict[str, list[str]] = {}
    collection_sets = {p.key: set(p.collections) for p in papers}
    tag_sets = {p.key: set(p.tags) for p in papers}
    for paper in papers:
        scored = []
        for other in papers:
            if other.key == paper.key:
                continue
            collection_overlap = collection_sets[paper.key] & collection_sets[other.key]
            tag_overlap = tag_sets[paper.key] & tag_sets[other.key]
            score = len(collection_overlap) * 3 + len(tag_overlap)
            if score <= 0:
                continue
            scored.append((score, other.year or "", other.title, other))
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        related[paper.key] = [
            f"{_obsidian_link(link_map[other.key], other.title)} ({other.year or 'n.d.'})"
            for _, _, _, other in scored[:8]
        ]
    return related


def _replace_managed_block(existing: str, managed_body: str) -> str:
    if BEGIN in existing and END in existing:
        pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
        return pattern.sub(lambda _: managed_body, existing, count=1)
    suffix = "\n\n" if existing and not existing.endswith("\n\n") else ""
    return existing.rstrip() + suffix + managed_body + "\n"


def _replace_frontmatter(existing: str, frontmatter: str) -> str:
    if not existing.startswith("---\n"):
        return frontmatter + "\n\n" + existing.lstrip()
    end = existing.find("\n---", 4)
    if end == -1:
        return frontmatter + "\n\n" + existing
    current = existing[: end + 4]
    rest = existing[end + 4 :].lstrip("\n")
    if f"managed_by: \"{MANAGED_BY}\"" in current or f"managed_by: {MANAGED_BY}" in current:
        return frontmatter + "\n\n" + rest
    return existing


def _write_text(path: Path, text: str, dry_run: bool, actions: list[str]) -> None:
    if dry_run:
        actions.append(f"计划写入 {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    actions.append(f"已写入 {path}")


def _write_managed_page(
    path: Path,
    title: str,
    managed_body: str,
    dry_run: bool,
    actions: list[str],
    frontmatter: str | None = None,
    manual_template: str | None = None,
) -> None:
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if frontmatter:
            text = _replace_frontmatter(text, frontmatter)
        if BEGIN in text:
            text = re.sub(r"^# .*$", f"# {title}", text, count=1, flags=re.M)
        text = _replace_managed_block(text, managed_body)
    else:
        header = f"# {title}\n\n"
        text = (frontmatter + "\n\n" if frontmatter else "") + header + managed_body
        if manual_template:
            text += "\n\n" + manual_template.rstrip() + "\n"
    _write_text(path, text, dry_run, actions)


def _write_literature_notes(
    output_dir: Path,
    papers: list[Paper],
    dry_run: bool,
    actions: list[str],
) -> dict[str, str]:
    literature_dir = output_dir / "literature"
    link_map = _literature_link_map(papers)
    related_map = _related_link_map(papers, link_map)
    for paper in papers:
        path = literature_dir / _paper_filename(paper)
        manual = "## 人工笔记\n\n\n## 可转化为 Wiki 的结论\n"
        _write_managed_page(
            path=path,
            title=paper.title,
            managed_body=_paper_managed_body(paper, link_map, related_map),
            dry_run=dry_run,
            actions=actions,
            frontmatter=_frontmatter(paper),
            manual_template=manual,
        )
    return link_map


def _topic_body(title: str, papers: list[Paper], link_map: dict[str, str]) -> str:
    lines = [
        BEGIN,
        f"_生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"## {title}",
        "",
        f"- 当前 wiki 导出中关联文献数：{len(papers)}",
        "",
        "## 文献列表",
        "",
    ]
    for paper in sorted(papers, key=lambda p: (p.year or "9999", p.title)):
        link = _obsidian_link(link_map[paper.key], paper.title)
        status = _status_badges(paper)
        lines.append(f"- {link} ({paper.year or 'n.d.'}) - {status}")
    lines.extend(["", END])
    return "\n".join(lines)


def _write_collection_pages(
    output_dir: Path,
    papers: list[Paper],
    link_map: dict[str, str],
    dry_run: bool,
    actions: list[str],
) -> None:
    by_collection: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        for collection in paper.collections or ["uncategorized"]:
            by_collection[collection].append(paper)
    for collection, group in sorted(by_collection.items(), key=lambda x: x[0].lower()):
        path = output_dir / "collections" / f"{_slug(collection)}.md"
        _write_managed_page(
            path,
            collection,
            _topic_body(collection, group, link_map),
            dry_run,
            actions,
            manual_template="## 人工整理\n",
        )


def _write_tag_pages(
    output_dir: Path,
    papers: list[Paper],
    link_map: dict[str, str],
    dry_run: bool,
    actions: list[str],
    max_pages: int,
) -> None:
    counts = Counter(tag for paper in papers for tag in paper.tags)
    selected = {tag for tag, count in counts.most_common(max_pages) if count >= 1}
    by_tag: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        for tag in paper.tags:
            if tag in selected:
                by_tag[tag].append(paper)
    for tag, group in sorted(by_tag.items(), key=lambda x: (-len(x[1]), x[0].lower())):
        path = output_dir / "topics" / f"{_slug(tag)}.md"
        _write_managed_page(
            path,
            tag,
            _topic_body(tag, group, link_map),
            dry_run,
            actions,
            manual_template="## 人工整理\n",
        )


def _last_ingest_stats() -> dict[str, Any]:
    path = config.DATA_DIR / "last_ingest_stats.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ensure_schema_file(output_dir: Path, dry_run: bool, actions: list[str]) -> None:
    target = output_dir / "AGENTS.md"
    if target.exists():
        return
    if not DEFAULT_SCHEMA_TEMPLATE.exists():
        return
    text = DEFAULT_SCHEMA_TEMPLATE.read_text(encoding="utf-8")
    _write_text(target, text, dry_run, actions)


def _write_index_and_status(
    output_dir: Path,
    papers: list[Paper],
    link_map: dict[str, str],
    chroma: dict[str, Any],
    dry_run: bool,
    actions: list[str],
) -> None:
    parsed_count = len(_parsed_keys())
    stats = _last_ingest_stats()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    index_lines = [
        BEGIN,
        f"_生成时间：{generated}_",
        "",
        "## Wiki 地图",
        "",
        f"- {_obsidian_link(_wiki_path('status'), '入库与索引状态')}",
        f"- {_obsidian_link(_wiki_path('AGENTS'), '维护规则 / Schema')}",
        f"- {_obsidian_link(_wiki_path('log'), '同步日志')}",
        f"- {_obsidian_link(_wiki_path('literature'), '单篇文献笔记')}",
        f"- {_obsidian_link(_wiki_path('collections'), 'Zotero 文件夹主题页')}",
        f"- {_obsidian_link(_wiki_path('topics'), '标签/主题页')}",
        "",
        "## 当前导出",
        "",
        f"- 单篇文献笔记：{len(papers)}",
        f"- parsed 缓存总数：{parsed_count}",
        f"- ChromaDB 集合数：{chroma.get('collection_count', 0)}",
        f"- ChromaDB 向量片段：{chroma.get('chunk_count', 0)}",
        "",
        "## 最近导出的笔记",
        "",
    ]
    for paper in papers[:30]:
        index_lines.append(f"- {_obsidian_link(link_map[paper.key], paper.title)} ({paper.year or 'n.d.'})")
    index_lines.extend(["", END])
    _write_managed_page(
        output_dir / "index.md",
        "Zotero Brain Wiki",
        "\n".join(index_lines),
        dry_run,
        actions,
        manual_template="## 人工目录\n",
    )

    status_lines = [
        BEGIN,
        f"_生成时间：{generated}_",
        "",
        "## 入库状态",
        "",
        f"- 最近统计文件：`{config.DATA_DIR / 'last_ingest_stats.json'}`",
        f"- 统计 total：{stats.get('total', '无')}",
        f"- 统计 success：{stats.get('success', '无')}",
        f"- 统计 chunks：{stats.get('chunks', '无')}",
        f"- parsed 缓存总数：{parsed_count}",
        f"- ChromaDB 向量片段总数：{chroma.get('chunk_count', 0)}",
        "",
        "## 最大的 ChromaDB 集合",
        "",
    ]
    for col in chroma.get("collections", [])[:20]:
        status_lines.append(f"- `{col['safe_name']}` / {col['name']}: {col['count']}")
    if chroma.get("error"):
        status_lines.extend(["", f"- ChromaDB 警告：`{chroma['error']}`"])
    status_lines.extend(["", END])
    _write_managed_page(
        output_dir / "status.md",
        "Zotero Brain Wiki 状态",
        "\n".join(status_lines),
        dry_run,
        actions,
        manual_template="## 人工检查记录\n",
    )


def _append_log(
    output_dir: Path,
    papers: list[Paper],
    chroma: dict[str, Any],
    args: argparse.Namespace,
    dry_run: bool,
    actions: list[str],
) -> None:
    path = output_dir / "log.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    query = args.query or "全部匹配"
    filters = []
    if args.collection:
        filters.append("collection=" + ",".join(args.collection))
    if args.tag:
        filters.append("tag=" + ",".join(args.tag))
    if args.parsed_only:
        filters.append("parsed_only=true")
    if args.chroma_only:
        filters.append("chroma_only=true")
    filter_text = "; ".join(filters) if filters else "无"
    entry = [
        f"## [{stamp}] sync | Zotero Brain wiki",
        "",
        f"- 查询：`{query}`",
        f"- 过滤：{filter_text}",
        f"- limit：{args.limit}",
        f"- 本次文献页：{len(papers)}",
        f"- ChromaDB 集合数：{chroma.get('collection_count', 0)}",
        f"- ChromaDB 向量片段：{chroma.get('chunk_count', 0)}",
        f"- 输出：`{args.output_dir}`",
        "",
    ]
    if papers:
        entry.append("- 文献：")
        for paper in papers[:20]:
            entry.append(f"  - `{paper.key}` {paper.title} ({paper.year or 'n.d.'})")
        if len(papers) > 20:
            entry.append(f"  - ... 另有 {len(papers) - 20} 篇")
        entry.append("")
    if dry_run:
        actions.append(f"计划追加日志 {path}")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = "# Zotero Brain Wiki 日志\n\n"
    _write_text(path, existing.rstrip() + "\n\n" + "\n".join(entry).rstrip() + "\n", dry_run, actions)


def build_wiki(args: argparse.Namespace) -> list[str]:
    actions: list[str] = []
    global VAULT_LINK_PREFIX
    VAULT_LINK_PREFIX = args.vault_link_prefix.strip("/")
    _ensure_schema_file(args.output_dir, args.dry_run, actions)
    papers = _load_papers(args.zotero_sqlite)
    parsed = _parsed_keys()
    for paper in papers:
        paper.parsed = paper.key in parsed

    papers = [
        paper
        for paper in papers
        if _matches_filters(
            paper,
            args.query,
            args.collection or [],
            args.tag or [],
            args.parsed_only,
            args.chroma_only,
        )
    ]
    if args.limit and args.limit > 0:
        papers = papers[: args.limit]

    _attach_chroma_state(papers, args.chunk_preview_limit)
    if args.chroma_only:
        papers = [paper for paper in papers if paper.chroma_collection]

    chroma = _chroma_summary()
    link_map = _write_literature_notes(args.output_dir, papers, args.dry_run, actions)
    _write_collection_pages(args.output_dir, papers, link_map, args.dry_run, actions)
    _write_tag_pages(args.output_dir, papers, link_map, args.dry_run, actions, args.max_topic_pages)
    _write_index_and_status(args.output_dir, papers, link_map, chroma, args.dry_run, actions)
    if not args.no_log:
        _append_log(args.output_dir, papers, chroma, args, args.dry_run, actions)
    return actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 Zotero Brain 文献库导出为 Obsidian wiki。")
    parser.add_argument("--zotero-sqlite", type=Path, default=DEFAULT_ZOTERO_SQLITE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--vault-link-prefix", default=DEFAULT_VAULT_LINK_PREFIX, help="Obsidian 内部链接前缀；例如 zotero-llm-wiki/wiki。")
    parser.add_argument("--query", default="", help="按题名、摘要、作者、collection、tag 等元数据筛选。")
    parser.add_argument("--collection", action="append", help="按 Zotero collection 名称片段筛选。")
    parser.add_argument("--tag", action="append", help="按 Zotero tag 名称片段筛选。")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="最多导出文献数；0 表示导出全部匹配项。")
    parser.add_argument("--parsed-only", action="store_true", help="只导出已有 parsed Markdown 的文献。")
    parser.add_argument("--chroma-only", action="store_true", help="只导出已经进入 ChromaDB 的文献。")
    parser.add_argument("--chunk-preview-limit", type=int, default=3)
    parser.add_argument("--max-topic-pages", type=int, default=40)
    parser.add_argument("--no-log", action="store_true", help="不向 wiki/log.md 追加同步记录。")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    actions = build_wiki(args)
    print(f"Zotero Brain wiki {'dry-run' if args.dry_run else '同步'}完成。")
    print(f"操作数：{len(actions)}")
    for action in actions[:50]:
        print(f"- {action}")
    if len(actions) > 50:
        print(f"- ... 另有 {len(actions) - 50} 项")


if __name__ == "__main__":
    main()
