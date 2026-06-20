# -*- coding: utf-8 -*-
"""
Zotero Sync - 从 Zotero 文献库拉取数据

功能：
  - 连接 Zotero Web API
  - 列出所有 Collection（文件夹）
  - 列出论文元数据（标题、作者、年份、DOI 等）
  - 下载 PDF 附件
  - 自动翻页，拉取全部论文
"""

import shutil
import logging
import time
import re
from pathlib import Path

import config
from pyzotero import zotero

logger = logging.getLogger(__name__)

# pyzotero single request limit
_PAGE_SIZE = 100
_PAGE_FETCH_RETRIES = 5

# Non-paper types, skip
_SKIP_TYPES = {"attachment", "note", "annotation"}

_PDF_ATTACHMENT_CACHE_READY = False
_PDF_ATTACHMENT_CACHE: dict[str, list[dict]] = {}


def _safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "attachment.pdf"


def _get_client() -> zotero.Zotero:
    """创建 Zotero 客户端"""
    return zotero.Zotero(
        config.ZOTERO_USER_ID,
        config.ZOTERO_LIBRARY_TYPE,
        config.ZOTERO_API_KEY,
    )


def _refresh_pdf_attachment_cache(raw_items: list) -> None:
    """Build parent item -> PDF attachment metadata from an all-items fetch."""
    global _PDF_ATTACHMENT_CACHE_READY, _PDF_ATTACHMENT_CACHE
    cache: dict[str, list[dict]] = {}
    for item in raw_items:
        data = item.get("data", {})
        if data.get("itemType") != "attachment":
            continue
        if data.get("contentType") != "application/pdf":
            continue
        parent = data.get("parentItem")
        if parent:
            cache.setdefault(parent, []).append(data)
    _PDF_ATTACHMENT_CACHE = cache
    _PDF_ATTACHMENT_CACHE_READY = True
    logger.info(f"PDF attachment cache ready: {sum(len(v) for v in cache.values())} PDFs for {len(cache)} parent items")


def _get_pdf_attachments(zot: zotero.Zotero, item_key: str) -> list[dict]:
    """Return PDF child attachment metadata, using the list_items cache when available."""
    if _PDF_ATTACHMENT_CACHE_READY:
        return _PDF_ATTACHMENT_CACHE.get(item_key, [])

    children = zot.children(item_key)
    attachments = []
    for child in children:
        data = child["data"]
        if data.get("contentType", "") == "application/pdf":
            attachments.append(data)
    return attachments


def list_collections(zot: zotero.Zotero | None = None) -> list[dict]:
    """
    列出 Zotero 中所有 Collection（文件夹）

    返回: [{"key": "ABC123", "name": "电池", "parent": ""}, ...]
    """
    if zot is None:
        zot = _get_client()

    collections = zot.collections()
    result = []
    for col in collections:
        data = col["data"]
        result.append({
            "key": data["key"],
            "name": data["name"],
            "parent": data.get("parentCollection", ""),
        })
    logger.info(f"找到 {len(result)} 个 Collection")
    return result


def _fetch_all_items(zot: zotero.Zotero, collection_key: str | None = None) -> list:
    """
    翻页拉取全部 items（pyzotero 单次最多 100 条）

    使用 Zotero API 的 start + limit 分页，直到拿完为止。
    """
    all_items = []
    start = 0

    while True:
        last_error = None
        for attempt in range(_PAGE_FETCH_RETRIES):
            try:
                if collection_key:
                    batch = zot.collection_items(collection_key, limit=_PAGE_SIZE, start=start)
                else:
                    batch = zot.items(limit=_PAGE_SIZE, start=start)
                break
            except Exception as exc:
                last_error = exc
                if attempt < _PAGE_FETCH_RETRIES - 1:
                    wait = min((attempt + 1) * 5, 60)
                    logger.warning(
                        "  Zotero page fetch failed at start=%s; retry %s/%s in %ss: %s",
                        start,
                        attempt + 1,
                        _PAGE_FETCH_RETRIES,
                        wait,
                        exc,
                    )
                    time.sleep(wait)
        else:
            raise RuntimeError(f"Zotero page fetch failed at start={start}") from last_error

        if not batch:
            break

        all_items.extend(batch)
        logger.info(f"  已拉取 {len(all_items)} 条 (本批 {len(batch)})")

        if len(batch) < _PAGE_SIZE:
            break  # 最后一页

        start += _PAGE_SIZE
        time.sleep(0.3)  # 避免触发 API 限速

    return all_items


def _item_has_pdf(zot: zotero.Zotero, item_key: str) -> bool:
    """
    检查某篇论文是否有 PDF 子附件

    Zotero 的 PDF 是以 child attachment 形式挂载的，
    需要调 children() API 查询。
    """
    try:
        return bool(_get_pdf_attachments(zot, item_key))
    except Exception as e:
        logger.debug(f"查询子条目失败 ({item_key}): {e}")
    return False


def list_items(
    zot: zotero.Zotero | None = None,
    collection_key: str | None = None,
    check_pdf: bool = True,
) -> list[dict]:
    """
    列出全部论文元数据（自动翻页）

    Args:
        collection_key: 指定 Collection 的 key，None 表示全部
        check_pdf: 是否逐篇检查有无 PDF（会额外调 children API）

    返回: [{
        "key": "ABC123",
        "title": "固态电解质...",
        "authors": ["Wang", "Li"],
        "year": 2024,
        "doi": "10.1038/...",
        "item_type": "journalArticle",
        "collection_names": ["示例研究主题"],
        "has_pdf": True,
        "abstract": "...",
    }, ...]
    """
    if zot is None:
        zot = _get_client()

    # Pull Collection mapping first
    col_map = {}
    for col in list_collections(zot):
        col_map[col["key"]] = col["name"]

    # Paginate to fetch all items
    raw_items = _fetch_all_items(zot, collection_key)
    if collection_key is None:
        _refresh_pdf_attachment_cache(raw_items)

    result = []
    total = len(raw_items)
    for idx, item in enumerate(raw_items):
        data = item["data"]

        # Skip non-paper types
        if data["itemType"] in _SKIP_TYPES:
            continue

        # Extract authors
        authors = []
        for creator in data.get("creators", []):
            if creator.get("creatorType") == "author":
                last = creator.get("lastName", "")
                first = creator.get("firstName", "")
                name = f"{last} {first}".strip()
                if name:
                    authors.append(name)

        # Extract year
        date_str = data.get("date", "")
        year = None
        if date_str:
            try:
                year = int(date_str[:4])
            except (ValueError, IndexError):
                pass

        # Determine Collection - use Zotero folder name directly
        collection_names = []
        for col_key in data.get("collections", []):
            col_name = col_map.get(col_key, "")
            if col_name:
                collection_names.append(col_name)

        if not collection_names:
            collection_names = [config.DEFAULT_COLLECTION]

        # Check for PDF attachments (via child items)
        has_pdf = False
        if check_pdf:
            has_pdf = _item_has_pdf(zot, data["key"])
            if idx % 20 == 0 and idx > 0:
                logger.info(f"  PDF 检查进度: {idx}/{total}")
            time.sleep(0.1)  # 避免 API 限速

        result.append({
            "key": data["key"],
            "title": data.get("title", ""),
            "authors": authors,
            "year": year,
            "doi": data.get("DOI", ""),
            "item_type": data["itemType"],
            "journal": data.get("publicationTitle", ""),
            "journal_abbreviation": data.get("journalAbbreviation", ""),
            "issn": data.get("ISSN", ""),
            "url": data.get("url", ""),
            "abstract": data.get("abstractNote", ""),
            "collection_names": collection_names,
            "has_pdf": has_pdf,
        })

    logger.info(f"找到 {len(result)} 篇论文")
    return result


def get_item_pdf_keys(zot: zotero.Zotero | None = None, item_key: str = "") -> list[str]:
    """获取某个论文下所有 PDF 附件的 key"""
    if zot is None:
        zot = _get_client()

    return [data["key"] for data in _get_pdf_attachments(zot, item_key) if data.get("key")]


def download_pdf(
    zot: zotero.Zotero | None = None,
    item_key: str = "",
    save_dir: Path | None = None,
) -> Path | None:
    """
    获取论文的 PDF 路径（不复制，直接返回原始路径）。

    查找顺序:
      1. Zotero linked_file 附件路径（data/papers/ 永久存储）
      2. parsed/{key}/{key}.md 旧缓存（向后兼容，PDF 可能已不存在）
      3. Zotero 本地 storage（~/Zotero/storage/）
      4. Zotero API 云端下载（→ 存到 data/papers/）

    Args:
        item_key: 论文的 Zotero key
        save_dir: (废弃，保留参数兼容)

    返回: PDF 文件路径，或 None（获取失败）
    """
    if zot is None:
        zot = _get_client()

    try:
        pdf_attachments = _get_pdf_attachments(zot, item_key)
    except Exception as e:
        logger.debug(f"附件查询失败: {e}")
        pdf_attachments = []

    # === 1. Zotero linked_file 附件路径（data/papers/ 永久存储）===
    for data in pdf_attachments:
        if data.get("linkMode") == "linked_file":
            linked_path = data.get("path", "")
            if linked_path and Path(linked_path).exists():
                logger.info(f"PDF found via linked_file: {linked_path}")
                return Path(linked_path)

    # === 2. parsed/{key}/ 旧缓存中的 PDF（向后兼容）===
    parsed_dir = config.PARSED_DIR / item_key
    if parsed_dir.is_dir():
        for f in parsed_dir.glob("*.pdf"):
            if f.stat().st_size > 1000:
                logger.info(f"PDF found in parsed cache: {f}")
                return f

    # === 3. Zotero 本地 storage ===
    if pdf_attachments:
        pdf_key = pdf_attachments[0]["key"]
        local_dir = config.ZOTERO_LOCAL_STORAGE / pdf_key
        if local_dir.is_dir():
            for f in local_dir.iterdir():
                if f.suffix.lower() == ".pdf":
                    logger.info(f"PDF found in Zotero storage: {f}")
                    return f

    # === 4. Zotero API 云端下载 → 存到 data/papers/（不再存 parsed/）===
    if pdf_attachments:
        pdf_data = pdf_attachments[0]
        pdf_key = pdf_data["key"]
        papers_dir = config.PAPERS_DIR
        papers_dir.mkdir(parents=True, exist_ok=True)
        try:
            filename = _safe_filename(pdf_data.get("filename") or f"{pdf_key}.pdf")
            if not filename.lower().endswith(".pdf"):
                filename = f"{filename}.pdf"
            final_path = papers_dir / f"{item_key}__{pdf_key}__{filename}"
            if final_path.exists() and final_path.stat().st_size > 1000:
                logger.info(f"PDF found in Zotero API cache: {final_path}")
                return final_path

            staging_dir = papers_dir / ".tmp" / f"{item_key}__{pdf_key}__{int(time.time() * 1000)}"
            staging_dir.mkdir(parents=True, exist_ok=True)
            try:
                zot.dump(pdf_key, path=str(staging_dir))
                candidates = [
                    p for p in staging_dir.rglob("*.pdf")
                    if p.is_file() and p.stat().st_size > 1000
                ]
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    if final_path.exists():
                        final_path.unlink()
                    shutil.move(str(candidates[0]), str(final_path))
                    logger.info(f"PDF downloaded from Zotero API: {final_path}")
                    return final_path
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception as e:
            logger.debug(f"API 下载失败: {e}")

    logger.error(f"无法获取 PDF: {item_key}")
    return None


def _get_linked_file_path(zot: zotero.Zotero, item_key: str) -> str | None:
    """检查论文的附件中是否有 linked_file 模式，返回其路径。"""
    try:
        for data in _get_pdf_attachments(zot, item_key):
            if data.get("linkMode") == "linked_file" and data.get("contentType") == "application/pdf":
                path = data.get("path", "")
                if path:
                    return path
    except Exception as e:
        logger.debug(f"linked_file 检查失败: {e}")
    return None



def update_linked_file_path(item_key: str, new_path: str, zot=None) -> bool:
    """
    Update Zotero linked_file attachment path after PDF has been archived.

    Args:
        item_key: parent item's Zotero key
        new_path: new absolute PDF path
        zot: Zotero client (optional)

    Returns: True if updated, False otherwise
    """
    if zot is None:
        zot = _get_client()
    try:
        children = zot.children(item_key)
        for child in children:
            data = child['data']
            if data.get('linkMode') == 'linked_file' and data.get('contentType') == 'application/pdf':
                data['path'] = str(new_path)
                zot.update_item(data)
                logger.info(f'Updated linked_file for {item_key}: {new_path}')
                return True
    except Exception as e:
        logger.warning(f'Failed to update linked_file for {item_key}: {e}')
    return False


def get_item_fulltext(zot: zotero.Zotero | None = None, item_key: str = "") -> str:
    """
    获取论文的全文内容（Zotero 内置的全文索引）

    注意：这只是 Zotero 自己的全文索引，质量有限。
    对于扫描版 PDF，需要走 MinerU 解析。
    """
    if zot is None:
        zot = _get_client()

    try:
        content = zot.fulltext_item(item_key)
        return content.get("content", "")
    except Exception:
        return ""


def list_folders(zot: zotero.Zotero | None = None) -> list[dict]:
    """
    列出 Zotero 中所有 Collection（文件夹）及其论文数量。

    返回: [{"key": "ABC123", "name": "示例研究主题", "item_count": 45}, ...]
    """
    if zot is None:
        zot = _get_client()

    collections = zot.collections()
    result = []
    for col in collections:
        data = col["data"]
        # 获取该 Collection 的论文数量
        try:
            items = zot.collection_items(data["key"], limit=1)
            # pyzotero 返回的 items 没有 total_results，需要单独查询
            # 用 items_top 获取总数更快
            count = len(zot.collection_items(data["key"], limit=_PAGE_SIZE))
        except Exception:
            count = 0
        result.append({
            "key": data["key"],
            "name": data["name"],
            "parent": data.get("parentCollection", ""),
            "item_count": count,
        })
    logger.info(f"找到 {len(result)} 个 Collection")
    return result


def create_folder(name: str, parent: str | None = None, zot: zotero.Zotero | None = None) -> str:
    """
    在 Zotero 中创建新 Collection（文件夹）。

    Args:
        name: 文件夹中文名
        parent: 父 Collection key（可选，创建子文件夹）
        zot: Zotero 客户端（可选）

    Returns: 新创建的 Collection key
    """
    if zot is None:
        zot = _get_client()

    # 检查是否已存在
    existing = list_collections(zot)
    for col in existing:
        if col["name"] == name:
            logger.info(f"Collection 已存在: {name} (key={col['key']})")
            return col["key"]

    # 创建新 Collection
    template = zot.item_template("collection")
    template["name"] = name
    if parent:
        template["parentCollection"] = parent

    response = zot.create_collections([template])
    if response.get("failed"):
        raise RuntimeError(f"创建 Collection 失败: {response['failed']}")

    new_key = response["success"]["0"]
    logger.info(f"Collection 已创建: {name} (key={new_key})")
    return new_key


def get_item_metadata(identifier: str, zot: zotero.Zotero | None = None) -> dict | None:
    """
    从 Zotero API 拉取单篇论文的完整 metadata。

    Args:
        identifier: 论文标识（Zotero key / DOI / 标题关键词）
        zot: Zotero 客户端（可选）

    Returns: {
        "key": "ABC123",
        "title": "...",
        "authors": ["Last First", ...],
        "year": 2024,
        "doi": "10.1038/...",
        "url": "...",
        "abstract": "...",
        "journal": "...",
        "volume": "...",
        "pages": "...",
        "issue": "...",
    } 或 None
    """
    if zot is None:
        zot = _get_client()

    # 尝试 1: 当作 Zotero key 直接查询
    try:
        item = zot.item(identifier)
        if item:
            data = item["data"]
            return _extract_full_metadata(data)
    except Exception:
        pass

    # 尝试 2: 按 DOI 查询（需要先拉取 items 再过滤）
    if identifier.startswith("10."):
        items = list_items(zot, check_pdf=False)
        for it in items:
            if it.get("doi", "").lower() == identifier.lower():
                # 重新拉取完整 item（list_items 返回的是精简版）
                try:
                    full_item = zot.item(it["key"])
                    return _extract_full_metadata(full_item["data"])
                except Exception:
                    pass

    # 尝试 3: 按标题关键词查询（取前 4 个单词搜索）
    search_terms = identifier.split()[:4]
    if search_terms:
        query = " ".join(search_terms)
        try:
            candidates = zot.items(q=query)
            for item in candidates:
                data = item["data"]
                if data.get("itemType") in _SKIP_TYPES:
                    continue
                # 精确匹配标题
                if data.get("title", "").lower() == identifier.lower():
                    return _extract_full_metadata(data)
            # 如果精确匹配失败，返回第一个候选
            for item in candidates:
                data = item["data"]
                if data.get("itemType") not in _SKIP_TYPES:
                    return _extract_full_metadata(data)
        except Exception as e:
            logger.warning(f"标题搜索失败: {e}")

    return None


def _extract_full_metadata(data: dict) -> dict:
    """从 Zotero item data 提取完整 metadata（用于 BibTeX 生成）"""
    # Extract authors
    authors = []
    for creator in data.get("creators", []):
        if creator.get("creatorType") == "author":
            last = creator.get("lastName", "")
            first = creator.get("firstName", "")
            name = f"{last} {first}".strip()
            if name:
                authors.append(name)

    # Extract year
    date_str = data.get("date", "")
    year = None
    if date_str:
        try:
            year = int(date_str[:4])
        except (ValueError, IndexError):
            pass

    return {
        "key": data.get("key", ""),
        "title": data.get("title", ""),
        "authors": authors,
        "year": year,
        "doi": data.get("DOI", ""),
        "url": data.get("url", ""),
        "abstract": data.get("abstractNote", ""),
        "journal": data.get("publicationTitle", ""),
        "volume": data.get("volume", ""),
        "pages": data.get("pages", ""),
        "issue": data.get("issue", ""),
    }


def sync_all(
    download_pdfs: bool = False,
) -> dict:
    """
    完整同步：拉取全部论文元数据 + 可选下载 PDF

    返回: {
        "collections": [...],
        "items": [...],
        "stats": {"total": 150, "with_pdf": 120, "no_pdf": 30},
        "by_collection": {"示例研究主题": 80, ...},
    }
    """
    zot = _get_client()

    collections = list_collections(zot)
    items = list_items(zot, check_pdf=True)

    # Statistics
    with_pdf = sum(1 for i in items if i["has_pdf"])
    by_collection = {}
    for item in items:
        for cn in item["collection_names"]:
            by_collection[cn] = by_collection.get(cn, 0) + 1

    pdfs_downloaded = 0
    pdfs_failed = 0

    if download_pdfs:
        for item in items:
            if not item["has_pdf"]:
                continue
            result = download_pdf(zot, item["key"])
            if result:
                pdfs_downloaded += 1
            else:
                pdfs_failed += 1

    return {
        "collections": collections,
        "items": items,
        "stats": {
            "total": len(items),
            "with_pdf": with_pdf,
            "no_pdf": len(items) - with_pdf,
        },
        "by_collection": by_collection,
        "pdfs_downloaded": pdfs_downloaded,
        "pdfs_failed": pdfs_failed,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=== Zotero 文献库同步 ===\n")

    # List Collections
    collections = list_collections()
    print(f"Collection 列表 ({len(collections)}):")
    for col in collections:
        print(f"  - {col['name']}")

    print()

    # Fetch all papers (skip PDF check, quick count first)
    items = list_items(check_pdf=False)
    print(f"论文总数: {len(items)}\n")

    # Count by Collection
    by_col = {}
    for item in items:
        for cn in item["collection_names"]:
            by_col[cn] = by_col.get(cn, 0) + 1
    print("按 Collection 分布:")
    for name, count in sorted(by_col.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count} 篇")

    print()

    # Show first 20 papers
    print("前 20 篇:")
    for i, item in enumerate(items[:20]):
        year = item["year"] or "?"
        authors = ", ".join(item["authors"][:2]) if item["authors"] else "Unknown"
        cols = ", ".join(item["collection_names"][:2])
        title = item["title"][:60]
        print(f"  {i+1:3d}. [{year}] {title} | {authors} | {cols}")

    if len(items) > 20:
        print(f"  ... 还有 {len(items) - 20} 篇")
