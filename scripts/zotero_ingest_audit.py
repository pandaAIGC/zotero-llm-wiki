# -*- coding: utf-8 -*-
"""Audit Zotero LLM Wiki ingest outputs without calling Zotero/MinerU APIs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import vector_store  # noqa: E402


DEFAULT_OUTPUT_DIR = Path(os.environ.get("ZOTERO_REVIEW_DIR", Path.home() / "zotero-llm-wiki"))
SIGNAL_PATTERNS = {
    "traceback": re.compile(r"Traceback \(most recent call last\):"),
    "read_timeout": re.compile(r"ReadTimeout|timed out", re.I),
    "connect_error": re.compile(r"ConnectError|SSL:|UNEXPECTED_EOF", re.I),
    "http_429": re.compile(r"HTTP/1\.1 429|too many requests|rate limit", re.I),
    "http_402": re.compile(r"HTTP/1\.1 402|resource package|quota|insufficient", re.I),
    "cn_quota": re.compile(r"余额不足|无可用资源包|额度|限流"),
    "stats_saved": re.compile(r"Stats saved:"),
    "phase_1": re.compile(r"Phase 1:"),
    "phase_2": re.compile(r"Phase 2:"),
    "phase_3": re.compile(r"Phase 3:"),
}
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]\s+([A-Z0-9]+):\s+(.*)")
FALLBACK_FAILURE_RE = re.compile(
    r"\[ERROR\]\s+([A-Z0-9]+): single-paper fallback also failed:\s+(.*)"
)
PARSE_FAILED_EMPTY_RE = re.compile(r"\[([A-Z0-9]+)\]\s+failed or empty")
NO_CHUNKS_RE = re.compile(r"\s([A-Z0-9]+): no chunks")
SENSITIVE_URL_PATTERNS = [
    re.compile(r"https://zoterofilestorage[^ ]+"),
    re.compile(r"https://mineru\.oss-cn-shanghai\.aliyuncs\.com/[^ ]+"),
    re.compile(r"https://cdn-mineru\.openxlab\.org\.cn/[^ ]+"),
]


def _sanitize_log_line(line: str) -> str:
    sanitized = line
    for pattern in SENSITIVE_URL_PATTERNS:
        sanitized = pattern.sub("<signed-or-temporary-url>", sanitized)
    return sanitized


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": str(exc)}


def _parsed_keys() -> set[str]:
    if not config.PARSED_DIR.exists():
        return set()
    return {
        p.name
        for p in config.PARSED_DIR.iterdir()
        if p.is_dir() and any(p.glob("*.md"))
    }


def _parsed_duplicate_hashes() -> list[dict[str, Any]]:
    if not config.PARSED_DIR.exists():
        return []
    by_size: dict[int, list[tuple[str, Path]]] = {}
    for parsed_dir in config.PARSED_DIR.iterdir():
        if not parsed_dir.is_dir():
            continue
        md_files = sorted(parsed_dir.glob("*.md"))
        if not md_files:
            continue
        md_path = md_files[0]
        try:
            size = md_path.stat().st_size
        except OSError:
            continue
        by_size.setdefault(size, []).append((parsed_dir.name, md_path))

    groups: dict[str, list[dict[str, Any]]] = {}
    for same_size_files in by_size.values():
        if len(same_size_files) < 2:
            continue
        for key, md_path in same_size_files:
            h = hashlib.sha256()
            with md_path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            head = ""
            try:
                head = " ".join(md_path.read_text(encoding="utf-8", errors="replace").splitlines()[:5])[:240]
            except Exception:
                head = ""
            groups.setdefault(digest, []).append({
                "key": key,
                "path": str(md_path),
                "size": md_path.stat().st_size,
                "head": head,
            })
    duplicates = [
        {"hash": digest, "count": len(items), "items": items}
        for digest, items in groups.items()
        if len(items) > 1
    ]
    duplicates.sort(key=lambda x: x["count"], reverse=True)
    return duplicates


def _collection_stats() -> list[dict[str, Any]]:
    try:
        return vector_store.list_collections()
    except Exception as exc:
        return [{"error": str(exc)}]


def _latest_review(output_dir: Path) -> dict[str, Any] | None:
    reviews = output_dir / "reviews"
    if not reviews.exists():
        return None
    files = sorted(reviews.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    path = files[0]
    return {
        "path": str(path),
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "size": path.stat().st_size,
    }


def _scan_log(log_path: Path | None, tail_limit: int) -> dict[str, Any]:
    if not log_path or not log_path.exists():
        return {"path": str(log_path) if log_path else None, "exists": False}

    signals = {name: 0 for name in SIGNAL_PATTERNS}
    last_progress = None
    fallback_failures: list[dict[str, str]] = []
    parse_failed_empty: list[str] = []
    no_chunks: list[str] = []
    line_count = 0
    tail: list[str] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            line_count += 1
            for name, pattern in SIGNAL_PATTERNS.items():
                if pattern.search(line):
                    signals[name] += 1
            match = PROGRESS_RE.search(line)
            if match:
                current, total, key, message = match.groups()
                last_progress = {
                    "current": int(current),
                    "total": int(total),
                    "key": key,
                    "message": message[:240],
                }
            failure_match = FALLBACK_FAILURE_RE.search(line)
            if failure_match:
                key, message = failure_match.groups()
                fallback_failures.append({
                    "key": key,
                    "message": _sanitize_log_line(message)[:500],
                })
            parse_failed_match = PARSE_FAILED_EMPTY_RE.search(line)
            if parse_failed_match:
                parse_failed_empty.append(parse_failed_match.group(1))
            no_chunks_match = NO_CHUNKS_RE.search(line)
            if no_chunks_match:
                no_chunks.append(no_chunks_match.group(1))
            tail.append(_sanitize_log_line(line))
            if len(tail) > tail_limit:
                tail.pop(0)

    return {
        "path": str(log_path),
        "exists": True,
        "modified": datetime.fromtimestamp(log_path.stat().st_mtime).isoformat(timespec="seconds"),
        "size": log_path.stat().st_size,
        "line_count": line_count,
        "signals": signals,
        "last_progress": last_progress,
        "fallback_failures": fallback_failures,
        "parse_failed_empty": parse_failed_empty,
        "no_chunks": no_chunks,
        "tail": tail,
    }


def _merge_companion_output_log(log: dict[str, Any], log_path: Path | None, tail_limit: int) -> dict[str, Any]:
    """Fold in the sibling .out.log because completion summaries are printed to stdout."""
    if not log_path or log_path.suffix != ".log" or not str(log_path).endswith(".err.log"):
        return log
    out_path = Path(str(log_path)[:-len(".err.log")] + ".out.log")
    if not out_path.exists():
        return log

    companion = _scan_log(out_path, tail_limit)
    log["companion_output_log"] = companion
    signals = log.setdefault("signals", {})
    for name, count in (companion.get("signals") or {}).items():
        signals[name] = int(signals.get(name) or 0) + int(count or 0)
    for key in ("fallback_failures", "parse_failed_empty", "no_chunks"):
        merged = list(log.get(key) or [])
        merged.extend(companion.get(key) or [])
        log[key] = merged
    return log


def _is_ingest_running() -> bool:
    if os.name != "nt":
        return False
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' -and $_.CommandLine -like '*run_ingest.py*' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return False
    return bool(result.stdout.strip())


def _decide_status(
    stats: dict[str, Any] | None,
    log: dict[str, Any],
    chroma_total: int,
    parsed_count: int,
    duplicate_hashes: list[dict[str, Any]],
    ingest_running: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    signals = log.get("signals") or {}

    if ingest_running and log.get("last_progress") and int(signals.get("stats_saved") or 0) == 0:
        progress = log["last_progress"]
        return "running", [
            "ingest log shows current progress but has not reached Stats saved yet",
            f"last progress: {progress.get('current')}/{progress.get('total')} {progress.get('key')}",
        ]

    hard_errors = [
        name for name in ("traceback", "http_429", "http_402", "cn_quota")
        if int(signals.get(name) or 0) > 0
    ]
    if hard_errors:
        reasons.append(f"hard error signals in log: {', '.join(hard_errors)}")

    if not stats:
        if ingest_running and log.get("last_progress"):
            return "running", ["ingest log has progress but stats were not refreshed yet"]
        return "fail", ["missing last_ingest_stats.json"]

    if log.get("last_progress") and int(signals.get("stats_saved") or 0) == 0:
        progress = log["last_progress"]
        reasons.append(
            "ingest process is not running, but the selected log ended before Stats saved"
        )
        reasons.append(
            f"last incomplete progress: {progress.get('current')}/{progress.get('total')} {progress.get('key')}"
        )

    if stats.get("_error"):
        return "fail", [f"stats JSON unreadable: {stats['_error']}"]

    if stats.get("api_limited"):
        reasons.append("last ingest stopped with api_limited=true")

    success = int(stats.get("success") or 0)
    chunks = int(stats.get("chunks") or 0)
    failed = int(stats.get("failed") or 0)
    stats_parse_failed_empty = int(stats.get("parse_failed_empty") or 0)
    stats_no_pdf_skipped = int(stats.get("no_pdf_skipped") or 0)
    stats_parse_failures_skipped = int(stats.get("parse_failures_skipped") or 0)
    stats_suspect_pdf_duplicates = int(stats.get("suspect_pdf_duplicates") or 0)
    no_actionable = bool(stats.get("no_actionable"))
    if not no_actionable:
        no_actionable = (
            success == 0
            and chunks == 0
            and failed == 0
            and not stats.get("api_limited")
            and chroma_total > 0
            and parsed_count > 0
            and (
                stats_no_pdf_skipped > 0
                or stats_parse_failures_skipped > 0
                or stats_suspect_pdf_duplicates > 0
            )
        )
    log_parse_failed_empty = sorted(set(log.get("parse_failed_empty") or []))
    log_no_chunks = sorted(set(log.get("no_chunks") or []))

    if success <= 0 and not no_actionable:
        reasons.append("last ingest success count is zero")
    if chunks <= 0 and not no_actionable:
        reasons.append("last ingest chunk count is zero")
    if chroma_total <= 0:
        reasons.append("ChromaDB has zero chunks")
    if parsed_count <= 0:
        reasons.append("parsed cache has zero parsed papers")
    if failed > 0:
        reasons.append(f"last ingest failed count is {failed}")
    if stats_parse_failed_empty > 0:
        reasons.append(f"last ingest MinerU failed/empty count is {stats_parse_failed_empty}")
    if no_actionable and stats_no_pdf_skipped > 0:
        reasons.append(f"last ingest had no actionable new PDFs ({stats_no_pdf_skipped} no-PDF skips)")
    if stats_parse_failures_skipped > 0:
        reasons.append(f"last ingest skipped {stats_parse_failures_skipped} previous parse failure(s)")
    if stats_suspect_pdf_duplicates > 0:
        reasons.append(
            f"last ingest skipped {stats_suspect_pdf_duplicates} suspicious duplicate PDF item(s)"
        )
    if log_parse_failed_empty:
        sample = ", ".join(log_parse_failed_empty[:12])
        reasons.append(f"log shows MinerU failed/empty for {len(log_parse_failed_empty)} key(s): {sample}")
    if log_no_chunks:
        sample = ", ".join(log_no_chunks[:12])
        reasons.append(f"log shows no chunks for {len(log_no_chunks)} key(s): {sample}")
    if duplicate_hashes:
        top = duplicate_hashes[0]
        reasons.append(
            f"parsed cache has {len(duplicate_hashes)} duplicate markdown hash group(s); "
            f"largest group has {top['count']} keys"
        )

    warning_prefixes = (
        "last ingest success count is zero",
        "last ingest chunk count is zero",
        "last ingest had no actionable new PDFs ",
        "parsed cache has ",
        "last ingest MinerU failed/empty count is ",
        "last ingest skipped ",
        "log shows MinerU failed/empty for ",
        "log shows no chunks for ",
    )
    warning_only = bool(reasons) and all(
        reason.startswith(warning_prefixes) for reason in reasons
    )

    if hard_errors or stats.get("api_limited"):
        return "warn", reasons
    if warning_only:
        return "warn", reasons
    if reasons:
        return "fail", reasons
    return "pass", ["stats, parsed cache, and ChromaDB all show successful output"]


def run(output_dir: Path, log_path: Path | None, tail_limit: int) -> tuple[Path, Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = output_dir / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    stats = _read_json(config.DATA_DIR / "last_ingest_stats.json")
    parsed = _parsed_keys()
    collections = _collection_stats()
    chroma_total = sum(int(c.get("count") or 0) for c in collections if "error" not in c)
    log = _merge_companion_output_log(_scan_log(log_path, tail_limit), log_path, tail_limit)
    review = _latest_review(output_dir)
    duplicate_hashes = _parsed_duplicate_hashes()
    suspect_pdf_report = config.DATA_DIR / "suspect_pdf_duplicates.json"
    ingest_running = _is_ingest_running()
    status, reasons = _decide_status(
        stats, log, chroma_total, len(parsed), duplicate_hashes, ingest_running
    )

    audit = {
        "created": now.isoformat(timespec="seconds"),
        "status": status,
        "reasons": reasons,
        "project": str(ROOT),
        "stats_path": str(config.DATA_DIR / "last_ingest_stats.json"),
        "stats": stats,
        "parsed_count": len(parsed),
        "parsed_duplicate_hashes": duplicate_hashes,
        "suspect_pdf_duplicate_report": str(suspect_pdf_report) if suspect_pdf_report.exists() else None,
        "chroma_total_chunks": chroma_total,
        "collections": collections,
        "latest_review": review,
        "ingest_running": ingest_running,
        "log": log,
    }

    stamp = now.strftime("%Y%m%d-%H%M%S")
    json_path = audit_dir / f"ingest-audit-{stamp}.json"
    md_path = audit_dir / f"ingest-audit-{stamp}.md"
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# Zotero LLM Wiki Ingest Audit {stamp}",
        "",
        f"- Status: `{status}`",
        f"- Project: `{ROOT}`",
        f"- Parsed cache count: {len(parsed)}",
        f"- Parsed duplicate hash groups: {len(duplicate_hashes)}",
        f"- ChromaDB chunks: {chroma_total}",
        f"- Last stats success/chunks/failed/api_limited: "
        f"{(stats or {}).get('success', 'n/a')} / {(stats or {}).get('chunks', 'n/a')} / "
        f"{(stats or {}).get('failed', 'n/a')} / {(stats or {}).get('api_limited', 'n/a')}",
        f"- Last stats MinerU failed/empty: {(stats or {}).get('parse_failed_empty', 'n/a')}",
        f"- Last stats skipped previous parse failures: {(stats or {}).get('parse_failures_skipped', 'n/a')}",
        f"- Last stats suspicious duplicate PDFs skipped: {(stats or {}).get('suspect_pdf_duplicates', 'n/a')}",
        f"- Suspicious duplicate report: `{suspect_pdf_report if suspect_pdf_report.exists() else 'missing'}`",
        f"- Latest review: `{review.get('path') if review else 'missing'}`",
        f"- Log: `{log.get('path')}`",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    lines += ["", "## Collections", ""]
    if collections:
        for col in collections:
            if "error" in col:
                lines.append(f"- ERROR: {col['error']}")
            else:
                lines.append(f"- {col.get('name')} (`{col.get('safe_name')}`): {col.get('count')} chunks")
    else:
        lines.append("- No collections found.")
    lines += ["", "## Parsed Duplicate Hashes", ""]
    if duplicate_hashes:
        for group in duplicate_hashes[:20]:
            keys = ", ".join(item["key"] for item in group["items"][:12])
            lines.append(f"- {group['hash']} ({group['count']} keys): {keys}")
    else:
        lines.append("- None.")
    lines += ["", "## Single-Paper Fallback Failures", ""]
    fallback_failures = log.get("fallback_failures") or []
    if fallback_failures:
        for failure in fallback_failures[:80]:
            lines.append(f"- {failure.get('key')}: {failure.get('message')}")
        if len(fallback_failures) > 80:
            lines.append(f"- ... {len(fallback_failures) - 80} more")
    else:
        lines.append("- None.")
    lines += ["", "## MinerU Failed Or Empty", ""]
    parse_failed_empty = log.get("parse_failed_empty") or []
    if parse_failed_empty:
        for key in sorted(set(parse_failed_empty))[:120]:
            lines.append(f"- {key}")
    else:
        lines.append("- None.")
    lines += ["", "## No Chunk Papers", ""]
    no_chunks = log.get("no_chunks") or []
    if no_chunks:
        for key in sorted(set(no_chunks))[:120]:
            lines.append(f"- {key}")
    else:
        lines.append("- None.")
    lines += ["", "## Log Tail", ""]
    lines.extend(f"    {line}" for line in (log.get("tail") or [])[-tail_limit:])
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, md_path, audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--tail-limit", type=int, default=80)
    args = parser.parse_args()

    json_path, md_path, audit = run(Path(args.output_dir), args.log, args.tail_limit)
    print(json.dumps({
        "status": audit["status"],
        "json": str(json_path),
        "markdown": str(md_path),
        "reasons": audit["reasons"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
