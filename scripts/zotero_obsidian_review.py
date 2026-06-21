# -*- coding: utf-8 -*-
"""Generate a Zotero review note for an Obsidian vault.

This runner is intentionally conservative: it reads Zotero and Zotero LLM Wiki
state, then writes a dated review note into a standalone Obsidian folder. It
does not modify Zotero, and it does not update concept pages automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import vector_store  # noqa: E402
import zotero_sync  # noqa: E402


DEFAULT_VAULT = Path(os.environ.get("OBSIDIAN_VAULT_DIR", Path.home()))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("ZOTERO_REVIEW_DIR", DEFAULT_VAULT / "zotero-llm-wiki"))
REVIEWS_DIR_NAME = "reviews"


HIGH_VALUE_PATTERNS = [
    r"\bguideline\b",
    r"\bguidelines\b",
    r"\brecommendation",
    r"\bconsensus\b",
    r"\bphase\s*(1|2|3|i|ii|iii)\b",
    r"\brandomi[sz]ed\b",
    r"\bclinical trial\b",
    r"\bmeta-analysis\b",
    r"\bsystematic review\b",
    r"\bNCCN\b",
    r"\bELN\b",
    r"\bESMO\b",
    r"\bEHA\b",
    r"指南",
    r"共识",
    r"推荐",
    r"随机",
    r"临床试验",
    r"系统综述",
    r"荟萃分析",
]


TOPIC_HINTS = {
    "AML": [r"\bAML\b", r"acute myeloid", r"急性髓系", r"急性髓细胞"],
    "MDS": [r"\bMDS\b", r"myelodysplastic", r"骨髓增生异常"],
    "ALL": [r"\bALL\b", r"acute lymphoblastic", r"急性淋巴"],
    "APL": [r"\bAPL\b", r"promyelocytic", r"早幼粒"],
    "B-cell lymphoma": [r"DLBCL", r"diffuse large B", r"B-cell lymphoma", r"弥漫大B", r"B细胞淋巴瘤"],
    "Multiple myeloma": [r"myeloma", r"multiple myeloma", r"骨髓瘤"],
    "CLL-SLL": [r"\bCLL\b", r"\bSLL\b", r"chronic lymphocytic", r"慢性淋巴细胞"],
    "CML": [r"\bCML\b", r"chronic myeloid", r"慢性髓"],
    "MPN": [r"\bMPN\b", r"myeloproliferative", r"骨髓增殖"],
    "HSCT": [r"HSCT", r"transplant", r"hematopoietic stem cell", r"造血干细胞移植"],
    "CAR-T": [r"CAR[- ]?T", r"chimeric antigen receptor", r"嵌合抗原受体"],
    "ITP": [r"\bITP\b", r"immune thrombocytopenia", r"免疫性血小板减少"],
    "Coagulation disorders": [r"hemophilia", r"coagulation", r"血友病", r"凝血"],
    "Pediatric": [r"pediatric", r"children", r"childhood", r"儿童", r"青少年"],
}


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _slug_date(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _zotero_link(key: str) -> str:
    return f"zotero://select/library/items/{key}"


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.I) for p in patterns)


def _topic_hints(item: dict[str, Any]) -> list[str]:
    text = " ".join([
        _safe_text(item.get("title")),
        _safe_text(item.get("abstract")),
        " ".join(item.get("collection_names") or []),
    ])
    return [topic for topic, patterns in TOPIC_HINTS.items() if _matches_any(text, patterns)]


def _is_high_value(item: dict[str, Any]) -> bool:
    text = " ".join([
        _safe_text(item.get("title")),
        _safe_text(item.get("abstract")),
        _safe_text(item.get("item_type")),
    ])
    return _matches_any(text, HIGH_VALUE_PATTERNS)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parsed_keys() -> set[str]:
    if not config.PARSED_DIR.exists():
        return set()
    return {
        p.name
        for p in config.PARSED_DIR.iterdir()
        if p.is_dir() and any(p.glob("*.md"))
    }


def _collection_stats() -> list[dict[str, Any]]:
    try:
        return vector_store.list_collections()
    except Exception:
        return []


def _collection_map(zot: Any) -> dict[str, str]:
    return {col["key"]: col["name"] for col in zotero_sync.list_collections(zot)}


def _item_from_raw(raw: dict[str, Any], col_map: dict[str, str]) -> dict[str, Any] | None:
    data = raw.get("data") or {}
    if data.get("itemType") in {"attachment", "note", "annotation"}:
        return None

    authors = []
    for creator in data.get("creators", []):
        if creator.get("creatorType") == "author":
            name = f"{creator.get('lastName', '')} {creator.get('firstName', '')}".strip()
            if name:
                authors.append(name)

    date_str = str(data.get("date") or "")
    year = None
    if date_str:
        match = re.search(r"(19|20)\d{2}", date_str)
        if match:
            year = int(match.group(0))

    return {
        "key": data.get("key", ""),
        "title": data.get("title", ""),
        "authors": authors,
        "year": year,
        "doi": data.get("DOI", ""),
        "url": data.get("url", ""),
        "item_type": data.get("itemType", ""),
        "collection_names": [col_map.get(k, k) for k in data.get("collections", [])],
        "has_pdf": False,
        "abstract": data.get("abstractNote", ""),
        "date_modified": data.get("dateModified", ""),
        "publication_title": data.get("publicationTitle", ""),
    }


def _fetch_recent_items(zot: Any, scan_limit: int) -> list[dict[str, Any]]:
    col_map = _collection_map(zot)
    out: list[dict[str, Any]] = []
    start = 0
    page_size = 100
    while len(out) < scan_limit:
        batch = zot.items(
            limit=min(page_size, scan_limit - len(out)),
            start=start,
            sort="dateModified",
            direction="desc",
        )
        if not batch:
            break
        for raw in batch:
            item = _item_from_raw(raw, col_map)
            if item is not None:
                out.append(item)
        if len(batch) < page_size:
            break
        start += page_size
    return out


def _format_item_line(item: dict[str, Any], parsed: set[str]) -> str:
    key = item.get("key", "")
    title = _safe_text(item.get("title")) or "(untitled)"
    year = item.get("year") or "n.d."
    doi = _safe_text(item.get("doi")) or "-"
    item_type = _safe_text(item.get("item_type")) or "-"
    pdf = "PDF" if item.get("has_pdf") else "no PDF"
    parsed_mark = "parsed" if key in parsed else "not parsed"
    topics = ", ".join(_topic_hints(item)) or "unrouted"
    collections = ", ".join(item.get("collection_names") or []) or "no collection"
    return (
        f"- [{title}]({_zotero_link(key)}) ({year}; `{item_type}`; `{key}`) "
        f"- {pdf}; {parsed_mark}; DOI: `{doi}`; Topics: {topics}; Collections: {collections}"
    )


def _hydrate_pdf_flags(zot: Any, items: list[dict[str, Any]]) -> None:
    """Check PDF status only for a bounded item subset."""
    seen: set[str] = set()
    for item in items:
        key = item.get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        item["has_pdf"] = zotero_sync._item_has_pdf(zot, key)


def _ensure_output(output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reviews = output_dir / REVIEWS_DIR_NAME
    reviews.mkdir(parents=True, exist_ok=True)
    index = output_dir / "index.md"
    log = output_dir / "log.md"
    if not index.exists():
        index.write_text(
            "# Zotero LLM Wiki 文献复盘\n\n"
            "这里保存 Zotero 定时复盘输出，用作进入 Med LLM-Wiki 前的筛选队列。\n\n"
            "## Reviews\n",
            encoding="utf-8",
        )
    if not log.exists():
        log.write_text("# Zotero LLM Wiki Review Log\n", encoding="utf-8")
    return index, log, reviews


def _update_index(index_path: Path, review_rel: str, date_text: str) -> None:
    text = index_path.read_text(encoding="utf-8")
    section = "## Reviews"
    line = f"- [[{review_rel}|{date_text} Zotero 文献复盘]] — Zotero 最新文献、PDF/解析/向量化状态与待纳入队列。Updated: {date_text}"
    if section not in text:
        text = text.rstrip() + f"\n\n{section}\n\n{line}\n"
    elif review_rel not in text:
        text = text.rstrip() + f"\n{line}\n"
    index_path.write_text(text, encoding="utf-8")


def _append_log(log_path: Path, date_text: str, review_rel: str, counts: dict[str, Any]) -> None:
    entry = f"""

---

## {date_text} zotero_review | automated literature review

- Source: Zotero Web API (`{config.ZOTERO_LIBRARY_TYPE}` library `{config.ZOTERO_USER_ID}`), Zotero LLM Wiki local state
- Created:
  - `{review_rel}.md`
- Updated:
  - `index.md`
  - `log.md`
- Counts:
  - recent_items: {counts.get("recent_items", 0)}
  - high_value_candidates: {counts.get("high_value_candidates", 0)}
  - no_pdf_candidates: {counts.get("no_pdf_candidates", 0)}
  - parsed_cache_count: {counts.get("parsed_cache_count", 0)}
  - chroma_collections: {counts.get("chroma_collections", 0)}
- Conflicts: —
- Needs review:
  - Review high-value candidates before integrating into disease concept pages.
  - Do not overwrite guideline recommendations with single-paper findings without source comparison.
"""
    log_path.write_text(log_path.read_text(encoding="utf-8").rstrip() + entry, encoding="utf-8")


def generate_review(output_dir: Path, recent_limit: int, candidates_limit: int, scan_limit: int) -> Path:
    now = datetime.now()
    date_text = _slug_date(now)
    index_path, log_path, reviews_dir = _ensure_output(output_dir)

    zot = zotero_sync._get_client()
    items = _fetch_recent_items(zot, scan_limit)
    recent_items = items[:recent_limit]
    parsed = _parsed_keys()
    collections = _collection_stats()
    last_stats = _read_json(config.DATA_DIR / "last_ingest_stats.json") or {}

    high_value = [item for item in items if _is_high_value(item)]
    high_value = high_value[:candidates_limit]
    _hydrate_pdf_flags(zot, recent_items + high_value)
    no_pdf = [item for item in recent_items if not item.get("has_pdf")]
    topic_counter = Counter(topic for item in recent_items for topic in _topic_hints(item))

    review_name = f"{date_text}-Zotero文献复盘.md"
    review_path = reviews_dir / review_name
    review_rel = f"{REVIEWS_DIR_NAME}/{review_name[:-3]}"

    total_chunks = sum(int(col.get("count") or 0) for col in collections)
    zhipu_note = "OK or not tested"
    if config.ZHIPU_API_KEY and total_chunks == 0 and last_stats.get("success", 0) == 0:
        zhipu_note = "Not yet producing vectors; check Zhipu balance/resource package if embedding returns HTTP 429 code 1113."

    lines = [
        "---",
        "type: zotero-review",
        f"created: {now.isoformat(timespec='seconds')}",
        "source: Zotero Web API; Zotero LLM Wiki local state",
        "status: automated_review",
        "---",
        "",
        f"# {date_text} Zotero 文献复盘",
        "",
        "## 摘要",
        "",
        f"- Zotero recent-paper scan count: {len(items)}",
        f"- Recent items reviewed: {len(recent_items)}",
        f"- High-value candidates flagged: {len(high_value)}",
        f"- Recent items without PDF: {len(no_pdf)}",
        f"- Parsed Markdown cache count: {len(parsed)}",
        f"- ChromaDB collections: {len(collections)}",
        f"- ChromaDB chunk count: {total_chunks}",
        f"- Embedding status note: {zhipu_note}",
        f"- Last ingest total candidates: {last_stats.get('total', 'n/a')}",
        f"- Last ingest parsed papers: {last_stats.get('parsed', 'n/a')}",
        f"- Last ingest successful papers: {last_stats.get('success', 'n/a')}",
        f"- Last ingest failed papers: {last_stats.get('failed', 'n/a')}",
        f"- Last ingest chunks added: {last_stats.get('chunks', 'n/a')}",
        f"- Last ingest API-limited stop: {last_stats.get('api_limited', 'n/a')}",
        f"- Last ingest high-impact gate: JIF >= {last_stats.get('min_impact_factor', 'n/a')}; Q1 required: {last_stats.get('require_q1', 'n/a')}",
        "",
        "## 主题路由速览",
        "",
    ]
    if topic_counter:
        for topic, count in topic_counter.most_common():
            lines.append(f"- {topic}: {count}")
    else:
        lines.append("- No clear hematology topic hints in the recent slice.")

    lines += [
        "",
        "## 最近 Zotero 条目",
        "",
    ]
    lines.extend(_format_item_line(item, parsed) for item in recent_items)

    lines += [
        "",
        "## 高价值候选",
        "",
    ]
    if high_value:
        lines.extend(_format_item_line(item, parsed) for item in high_value)
    else:
        lines.append("- No guideline / consensus / trial / review candidates matched in this run.")

    lines += [
        "",
        "## PDF / 解析待处理",
        "",
    ]
    if no_pdf:
        lines.extend(_format_item_line(item, parsed) for item in no_pdf)
    else:
        lines.append("- Recent slice has no obvious PDF gaps.")

    lines += [
        "",
        "## Zotero LLM Wiki 状态",
        "",
        f"- Project: `{ROOT}`",
        f"- ChromaDB: `{config.CHROMA_DIR}`",
        f"- Parsed cache: `{config.PARSED_DIR}`",
        f"- Last ingest stats: `{config.DATA_DIR / 'last_ingest_stats.json'}`",
        "",
        "### ChromaDB Collections",
        "",
    ]
    if collections:
        for col in collections:
            lines.append(f"- {col.get('name')} (`{col.get('safe_name')}`): {col.get('count')} chunks")
    else:
        lines.append("- No vector collections currently populated.")

    lines += [
        "",
        "## Integration Boundary",
        "",
        "- This note is a review queue, not a clinical recommendation page.",
        "- Before writing into disease pages, compare candidate literature against existing NCCN / Chinese guideline framework.",
        "- Zotero is read-only in this workflow.",
        "",
        "## Sources",
        "",
        f"- Zotero Web API library `{config.ZOTERO_USER_ID}`",
        f"- Zotero LLM Wiki project `{ROOT}`",
        f"- Obsidian output folder `{output_dir}`",
    ]

    review_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    counts = {
        "recent_items": len(recent_items),
        "high_value_candidates": len(high_value),
        "no_pdf_candidates": len(no_pdf),
        "parsed_cache_count": len(parsed),
        "chroma_collections": len(collections),
    }
    _update_index(index_path, review_rel, date_text)
    _append_log(log_path, date_text, review_rel, counts)
    return review_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=str(DEFAULT_VAULT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--recent-limit", type=int, default=20)
    parser.add_argument("--candidates-limit", type=int, default=20)
    parser.add_argument("--scan-limit", type=int, default=300)
    args = parser.parse_args()

    vault = Path(args.vault)
    if not vault.exists():
        raise SystemExit(f"Med vault not found: {vault}")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = vault / output_dir
    path = generate_review(output_dir, args.recent_limit, args.candidates_limit, args.scan_limit)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
