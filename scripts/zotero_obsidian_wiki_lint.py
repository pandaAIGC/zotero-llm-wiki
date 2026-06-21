"""Health-check the Zotero LLM Wiki Obsidian wiki.

This is a read-only linter for the wiki content, except for writing a timestamped
lint report under wiki/lint and appending a short entry to wiki/log.md.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


DEFAULT_WIKI_DIR = Path(os.environ.get("ZOTERO_LLM_WIKI_DIR", Path.home() / "zotero-llm-wiki" / "wiki"))
DEFAULT_VAULT_LINK_PREFIX = os.environ.get("ZOTERO_LLM_WIKI_LINK_PREFIX", "zotero-llm-wiki/wiki").strip("/")
BEGIN = "<!-- ZOTERO_LLM_WIKI:BEGIN -->"
END = "<!-- ZOTERO_LLM_WIKI:END -->"


@dataclass
class WikiPage:
    path: Path
    rel: str
    text: str
    links: list[str] = field(default_factory=list)


def _vault_link_to_rel(link: str, prefixes: list[str]) -> str:
    target = link.split("|", 1)[0].split("#", 1)[0].strip()
    target = target.replace("\\", "/")
    for prefix in prefixes:
        prefix = prefix.strip("/")
        if prefix and target.startswith(prefix + "/"):
            target = target[len(prefix) + 1 :]
            break
    if target.endswith(".md"):
        target = target[:-3]
    return target.strip("/")


def _page_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).with_suffix("").as_posix()


def _load_pages(root: Path) -> list[WikiPage]:
    pages = []
    for path in sorted(root.rglob("*.md")):
        if ".tmp" in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        links = re.findall(r"\[\[([^\]]+)\]\]", text)
        pages.append(WikiPage(path=path, rel=_page_rel(path, root), text=text, links=links))
    return pages


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip().strip('"')
    return result


def _append_log(root: Path, status: str, issue_count: int, report_rel: str, dry_run: bool) -> None:
    if dry_run:
        return
    path = root / "log.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Zotero LLM Wiki 日志\n\n"
    entry = "\n".join(
        [
            f"## [{stamp}] lint | {status}",
            "",
            f"- 问题数：{issue_count}",
            f"- 报告：[[{report_rel}|{Path(report_rel).name}]]",
            "",
        ]
    )
    path.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")


def lint(root: Path, vault_link_prefix: str) -> tuple[str, list[str], dict[str, int]]:
    pages = _load_pages(root)
    by_rel = {p.rel: p for p in pages}
    inbound: Counter[str] = Counter()
    issues: list[str] = []
    prefixes = [vault_link_prefix, "zotero-llm-wiki/wiki", "ZoteroLLMWiki/wiki"]
    stats = {
        "pages": len(pages),
        "literature": 0,
        "collections": 0,
        "topics": 0,
        "broken_links": 0,
        "orphans": 0,
        "missing_frontmatter": 0,
        "missing_managed_block": 0,
        "missing_manual_area": 0,
    }

    for page in pages:
        if page.rel.startswith("literature/"):
            stats["literature"] += 1
        elif page.rel.startswith("collections/"):
            stats["collections"] += 1
        elif page.rel.startswith("topics/"):
            stats["topics"] += 1
        for raw_link in page.links:
            rel = _vault_link_to_rel(raw_link, prefixes)
            if not rel:
                continue
            inbound[rel] += 1
            if rel not in by_rel and not (root / f"{rel}.md").exists() and not (root / rel).is_dir():
                stats["broken_links"] += 1
                issues.append(f"- 断链：`{page.rel}` -> `[[{raw_link}]]`")

    for page in pages:
        if page.rel.startswith("literature/"):
            fm = _frontmatter(page.text)
            if not fm:
                stats["missing_frontmatter"] += 1
                issues.append(f"- 文献页缺 frontmatter：`{page.rel}`")
            else:
                for field in ["zotero_key", "title", "type"]:
                    if not fm.get(field):
                        stats["missing_frontmatter"] += 1
                        issues.append(f"- 文献页 frontmatter 缺 `{field}`：`{page.rel}`")
            if "## 人工笔记" not in page.text:
                stats["missing_manual_area"] += 1
                issues.append(f"- 文献页缺人工笔记区：`{page.rel}`")

        if page.rel.startswith(("literature/", "collections/", "topics/")) or page.rel in {"index", "status"}:
            if BEGIN not in page.text or END not in page.text:
                stats["missing_managed_block"] += 1
                issues.append(f"- 缺 managed block：`{page.rel}`")

        if page.rel.startswith(("literature/", "collections/", "topics/")) and inbound[page.rel] == 0:
            stats["orphans"] += 1
            issues.append(f"- 孤立页：`{page.rel}` 没有入链")

    status = "pass" if not issues else "needs_review"
    return status, issues, stats


def write_report(root: Path, status: str, issues: list[str], stats: dict[str, int], dry_run: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = root / "lint"
    report_path = report_dir / f"{stamp}-wiki-lint.md"
    lines = [
        f"# Zotero LLM Wiki 体检报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 状态：{status}",
        f"- 问题数：{len(issues)}",
        f"- 页面总数：{stats['pages']}",
        f"- 文献页：{stats['literature']}",
        f"- Collection 页：{stats['collections']}",
        f"- Topic 页：{stats['topics']}",
        f"- 断链：{stats['broken_links']}",
        f"- 孤立页：{stats['orphans']}",
        f"- frontmatter 问题：{stats['missing_frontmatter']}",
        f"- managed block 问题：{stats['missing_managed_block']}",
        f"- 人工笔记区问题：{stats['missing_manual_area']}",
        "",
        "## 问题清单",
        "",
    ]
    if issues:
        lines.extend(issues)
    else:
        lines.append("- 未发现需要处理的问题。")
    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 Zotero LLM Wiki Obsidian wiki 的链接和维护状态。")
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    parser.add_argument("--vault-link-prefix", default=DEFAULT_VAULT_LINK_PREFIX)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    status, issues, stats = lint(args.wiki_dir, args.vault_link_prefix)
    report = write_report(args.wiki_dir, status, issues, stats, args.dry_run)
    rel = report.relative_to(args.wiki_dir).with_suffix("").as_posix()
    _append_log(args.wiki_dir, status, len(issues), rel, args.dry_run)
    print(f"Zotero LLM Wiki wiki 体检完成：{status}")
    print(f"问题数：{len(issues)}")
    print(f"报告：{report}")


if __name__ == "__main__":
    main()
