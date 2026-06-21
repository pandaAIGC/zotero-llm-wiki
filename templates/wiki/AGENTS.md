# Zotero LLM Wiki 维护规则

本目录是 Zotero LLM Wiki 在 Obsidian 中维护的长期文献 wiki。它不是原始文献库，也不是最终结论库，而是介于 Zotero/parsed/ChromaDB 与人工知识整理之间的可增长 Markdown 知识层。

## 三层架构

- 原始来源：Zotero 元数据、PDF、`parsed/`、`data/chroma_db/`。这些是事实来源，不在 wiki 维护时修改。
- Wiki：由 `scripts/zotero_obsidian_wiki.py` 生成的 Markdown 页面。
- Schema：本文件、项目 `CODEX_HANDOFF.md`、项目 README。修改 wiki 结构或规则时应同步更新这些入口。

## 目录约定

- `index.md`：内容索引和导航入口。
- `status.md`：入库、parsed、ChromaDB 状态概览。
- `log.md`：按时间追加记录同步、专题整理、人工审核、lint 结果。
- `literature/`：一篇 Zotero 条目对应一个文献笔记。
- `collections/`：Zotero collection 主题页。
- `topics/`：Zotero tag 或人工主题页。
- `lint/`：知识库体检报告。

## 写作规则

- 用户可见内容优先中文。
- 原始论文题名、作者、期刊、DOI、URL、Zotero key 保留原文，避免引用信息失真。
- 技术名如 Zotero、ChromaDB、parsed、MinerU 可以保留英文。
- 不把 LLM 总结当作事实；机制结论、临床结论、方法学判断必须能回到具体文献页或 Zotero key。
- 临床/科研结论要写证据等级和不确定性，不能把单篇结果扩展成通用结论。

## 生成区块与人工区块

脚本维护的内容位于：

```markdown
<!-- ZOTERO_LLM_WIKI:BEGIN -->
...
<!-- ZOTERO_LLM_WIKI:END -->
```

再次同步时只更新该区块和受管页面标题/frontmatter。人工笔记、人工整理、可转化为 Wiki 的结论等区块必须保留。

## 工作流

### Ingest

1. 从 Zotero/parsed/ChromaDB 读取元数据和片段。
2. 生成或更新 `literature/` 文献页。
3. 更新相关 `collections/` 与 `topics/` 页面。
4. 更新 `index.md`、`status.md`。
5. 向 `log.md` 追加同步记录。

### Query

1. 先读 `index.md` 和相关 collection/topic 页。
2. 再读具体 literature 页。
3. 若需要更细证据，再使用 Zotero LLM Wiki MCP 或 parsed/ChromaDB 查原文片段。
4. 有价值的问题答案可以沉淀为新的 topic 页面或人工整理区。

### Lint

定期运行：

```powershell
python scripts/zotero_obsidian_wiki_lint.py --wiki-dir <your-wiki-dir>
```

重点检查：缺 frontmatter、缺 Zotero key、缺 managed block、断链、孤立页、无人工笔记区、状态页过期、主题页只有单个来源但没有人工整理。

## 边界

- 不在 wiki 维护流程中启动 `run_ingest.py`。
- 不删除 `parsed/`。
- 不修改 Zotero 数据库或 Zotero storage。
- 不提交 `.env`、`data/`、`parsed/` 或本地 Obsidian vault 内容。
