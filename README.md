# Zotero LLM Wiki

> 把 Zotero 文献库变成可语义搜索、可 AI 对话、可沉淀到 Obsidian 的 LLM Wiki。

> Built on an MIT-licensed Zotero literature MCP / ingestion core, with an added Obsidian LLM Wiki layer.

> **For AI Agents:** 如果你是通过 MCP 使用本项目的 AI Agent，请先阅读 [SKILL.md](SKILL.md)。它包含项目架构、文件布局、操作规范和场景速查，能帮你避免常见错误（如找不到 PDF、重复下载、混淆缓存路径等）。

## 这是什么？

Zotero 管理论文很好用，但它本质上是个死数据库——只能按标题、作者、标签搜索，不能按**意思**搜索。

Zotero LLM Wiki 解决这个问题：

```
你的 Zotero 文献库
    ↓
MinerU Cloud API（PDF → 结构化 Markdown）
    ↓
文本切块 → 智谱 Embedding 向量化 → ChromaDB 向量数据库
    ↓
MCP Server（11 个工具）
    ↓
AI Agent（WorkBuddy / Cursor / 任何 MCP 兼容客户端）
    ↓
Obsidian LLM Wiki（可长期增长、可自检、可人工沉淀的 Markdown 知识库）
```

**一句话：** 你用自然语言问 AI "关于材料 A 和通路 B 的关系，库里有哪些证据？"，AI 能从 Zotero 里语义搜索、定位段落、深入阅读、生成引用，并把值得保留的内容沉淀为 Obsidian wiki。

## 核心能力

| 你想知道/做的事 | 怎么做 |
|---|---|
| "我们库里有哪些关于 LLZO 电解质的论文？" | `search_papers` — 语义搜索，不是关键词搜索 |
| "这篇论文的方法部分具体怎么做的？" | `get_paper_chunks` + `expand_context` — 精确定位并深入阅读 |
| "把这篇论文全文调出来，我要逐段讨论" | `read_paper_full` — 读取完整 Markdown 缓存 |
| "帮我找最新的示例主题论文" | `discover_papers` — 搜 OpenAlex/arXiv/CrossRef/Semantic Scholar |
| "找到一篇好论文，帮我下载并入库" | `download_paper` → `import_to_zotero` → `ingest_paper` — 三步完成 |
| "我正在写论文，帮我推荐相关引用" | `get_bibtex(mode="recommend")` — 语义推荐 + BibTeX |
| "这篇论文的 BibTeX 是什么？" | `get_bibtex(mode="exact")` — 从 Zotero 拉取完整引用信息 |
| "我的文献库有哪些文件夹？哪些已同步？" | `list_collections` — 同时显示 Zotero 文件夹 + 向量库状态 |
| "新建一个文件夹来放新方向的论文" | `create_collection` — 同时创建 Zotero 文件夹 + 向量库 |
| "把 Zotero 文献沉淀成 Obsidian 知识库" | `scripts/zotero_obsidian_wiki.py` — 生成 LLM Wiki |
| "检查 Obsidian wiki 是否健康" | `scripts/zotero_obsidian_wiki_lint.py` — 查断链、孤立页、缺元数据 |

## 使用场景

### 场景 1：快速查阅文献库

你和 AI 说："关于火焰喷雾热解合成纳米颗粒，库里有什么论文讨论了前驱体浓度对粒径的影响？"

AI 会语义搜索你的文献库，找到相关段落，展开上下文给你详细解释。不需要你自己一篇篇翻。

### 场景 2：新论文入库

你说："帮我找 2025 年关于 self-driving lab 的最新论文，下载并入库到'自动化实验室'文件夹。"

AI 搜索学术数据库 → 下载 PDF → 导入 Zotero → OCR 解析 → 向量化入库。一条命令搞定。

### 场景 3：AI 辅助写作

你在写论文的 Introduction，说："关于自动化实验室在材料合成中的应用，帮我推荐引用。"

AI 语义搜索你的文献库 → 找到最相关的论文 → 深入阅读关键段落 → 给你推荐 + BibTeX + 段落摘要。

### 场景 4：精准对比

你说："对比一下 Wang 2024 和 Li 2025 这两篇关于固态电解质的方法和结论。"

AI 分别读取两篇论文的相关段落，提取关键信息进行结构化对比。

## 安装

### 前置条件

- **Python 3.13+**（推荐用 venv 虚拟环境）
- **Zotero**（本地安装，有你的文献库）
- ##### **能翻墙的网络**（OpenAlex、Unpaywall、CrossRef 等 API 需要访问境外服务器，**必须开启 TUN 模式代理**，见下方网络配置）

### 步骤 1：克隆项目

```bash
git clone <your-repo-url>
cd zotero-llm-wiki
```

### 步骤 2：安装依赖

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 步骤 3：配置 API 密钥

复制 `.env.example` 为 `.env`，填入你的 API Key（见下方申请指南）：

```bash
# ============================================================
# Zotero LLM Wiki - API 密钥配置
# ============================================================

# Zotero Web API（必填）
ZOTERO_USER_ID=你的用户ID
ZOTERO_API_KEY=你的API密钥
ZOTERO_LIBRARY_TYPE=user

# MinerU（必填 - PDF 解析）
MINERU_TOKEN=你的MinerU Token
MINERU_MODEL=vlm
MIN_PARSED_CACHE_CHARS=500

# 成本保护：同一 PDF 多次解析为空或无有效 chunk 后，后续增量入库会跳过
PARSE_FAILURE_MAX_ATTEMPTS=2

# 文本向量化（二选一）
# 默认: zhipu，使用智谱 embedding-3，写入 data/chroma_db
EMBED_PROVIDER=zhipu
ZHIPU_API_KEY=你的智谱API密钥

# 可选: ollama，使用本地 Ollama embedding，默认写入独立 ChromaDB
# 不要和智谱 2048 维向量混入同一个 ChromaDB。
# 推荐本机已有模型: qwen3-embedding:latest
# EMBED_PROVIDER=ollama
# OLLAMA_BASE_URL=http://127.0.0.1:11434
# OLLAMA_EMBED_MODEL=qwen3-embedding:latest

# CORE API（可选 - Open Access PDF 搜索）
CORE_API_KEY=你的CORE API密钥
```

### 步骤 4：配置 MCP 客户端

在你的 MCP 客户端（如 WorkBuddy、Cursor）中添加 Zotero LLM Wiki：

**WorkBuddy：** 在设置 → 连接器 → MCP 中添加，command 为：
```
.venv\Scripts\python.exe mcp_server.py
```
工作目录设为本项目路径。

**Cursor / 其他 MCP 客户端：** 在 `mcp.json` 中添加：
```json
{
  "mcpServers": {
    "zotero-llm-wiki": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/zotero-llm-wiki"
    }
  }
}
```

### 步骤 5：首次批量入库

将你现有的 Zotero 文献库全部向量化：

```bash
.venv\Scripts\python.exe run_ingest.py
```

这会把所有有 PDF 的论文解析并入库。之后新增的论文只需逐个 `ingest_paper` 即可。

如果想控制 MinerU 解析量或 embedding 花费，可以加预算上限。达到上限后脚本会停在下一篇解析/embedding API 调用前，并正常保存 stats：

```bash
.venv\Scripts\python.exe run_ingest.py --incremental --max-parse-papers 20
.venv\Scripts\python.exe run_ingest.py --incremental --max-embed-papers 20
.venv\Scripts\python.exe run_ingest.py --incremental --max-embed-chunks 2000
```

如果使用本地 Ollama embedding，建议先建一个独立索引，不覆盖现有智谱索引：

```bash
set EMBED_PROVIDER=ollama
set OLLAMA_EMBED_MODEL=qwen3-embedding:latest
.venv\Scripts\python.exe run_ingest.py --incremental --max-embed-papers 20
```

默认会写到 `data/chroma_db_ollama_qwen3-embedding_latest` 这类独立目录。不同 embedding 模型的向量维度不同，不能混在同一个 ChromaDB 里。

对反复解析为空、或解析后没有有效 chunk 的 PDF，脚本会写入 `data/parse_failures.json`。同一条目达到 `PARSE_FAILURE_MAX_ATTEMPTS` 后，后续增量入库会直接跳过，避免把 MinerU 和 embedding 额度浪费在已确认不可自动修复的残余项上。把该值设为 `0` 可以关闭这个保护。

### 步骤 6：生成 Obsidian LLM Wiki

本增强版支持把 Zotero LLM Wiki 读到的元数据、parsed 缓存和 ChromaDB 状态沉淀为 Obsidian Markdown wiki。

```bash
.venv\Scripts\python.exe scripts/zotero_obsidian_wiki.py ^
  --output-dir <YOUR_OBSIDIAN_WIKI_DIR> ^
  --query "示例主题 A" ^
  --limit 50 ^
  --chunk-preview-limit 3
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--output-dir` | Obsidian wiki 输出目录 |
| `--query` | 按题名、摘要、作者、collection、tag 等元数据筛选 |
| `--collection` | 按 Zotero collection 名称片段筛选，可重复传入 |
| `--tag` | 按 Zotero tag 名称片段筛选，可重复传入 |
| `--parsed-only` | 只导出已有 MinerU Markdown 缓存的文献 |
| `--chroma-only` | 只导出已经进入 ChromaDB 的文献 |
| `--limit 0` | 导出所有匹配项 |
| `--dry-run` | 只预览将写入的页面，不落盘 |

生成后的 wiki 结构：

```
wiki/
├── AGENTS.md             # wiki 维护规则 / schema
├── index.md              # 导航入口
├── status.md             # 入库和索引状态
├── log.md                # 同步与 lint 时间线
├── literature/           # 单篇文献笔记
├── collections/          # Zotero 文件夹主题页
├── topics/               # 标签/主题页
└── lint/                 # 体检报告
```

脚本只读 Zotero、`parsed/` 和 ChromaDB，不启动 `run_ingest.py`，不调用 MinerU/Zhipu，不修改 Zotero。生成页使用 managed block：

```markdown
<!-- ZOTERO_LLM_WIKI:BEGIN -->
...
<!-- ZOTERO_LLM_WIKI:END -->
```

再次同步时只更新 managed block、受管标题和 frontmatter；区块外的人工笔记会保留。

### 步骤 7：体检 Obsidian Wiki

```bash
.venv\Scripts\python.exe scripts/zotero_obsidian_wiki_lint.py ^
  --wiki-dir <YOUR_OBSIDIAN_WIKI_DIR>
```

lint 会检查：

- 断链
- 孤立页
- 文献页缺 frontmatter / Zotero key
- 缺 managed block
- 文献页缺人工笔记区

报告写入 `wiki/lint/`，并向 `wiki/log.md` 追加体检记录。

### 步骤 8：诊断重复 parsed 缓存

如果入库审计提示 `parsed cache has duplicate markdown hash group(s)`，可以生成一份带 Zotero 元数据的中文分级报告：

```bash
.venv\Scripts\python.exe scripts/zotero_parsed_duplicate_report.py ^
  --output-dir <YOUR_OBSIDIAN_WIKI_DIR> ^
  --zotero-sqlite <YOUR_ZOTERO_SQLITE>
```

报告会写入 `audits/parsed-duplicate-report-*.md/json/csv`，按 DOI、题名、parsed 头部匹配度分为“可能重复”“需要核对 Zotero key”“可疑错配”等类别。这个脚本只读 Zotero SQLite、审计 JSON 和 `parsed/`，不会删除缓存，也不会修改 Zotero。

## API Key 申请指南

| API | 用途 | 申请地址 | 费用 |
|---|---|---|---|
| **Zotero Web API** | 读写你的文献库 | https://www.zotero.org/settings/keys | 免费 |
| **MinerU** | PDF → 结构化 Markdown | https://mineru.net （注册后获取 Token） | 免费 |
| **智谱 BigModel** | 文本向量化（Embedding-3） | https://open.bigmodel.cn （注册后获取 API Key） | ¥0.0007/千 token |
| **CORE API** | Open Access PDF 搜索（可选） | https://core.ac.uk/services/api | 免费（需申请） |

> **OpenAlex 和 Unpaywall 不需要 Key**，它们是免费开放的 API。

## ⚠️ 网络配置（必读）

本项目的 API 分为两类，对网络有**完全不同的要求**：

| 类型 | API | 网络要求 |
|------|-----|---------|
| 🇨🇳 国内 API | **MinerU**（mineru.net）、**智谱 BigModel**（open.bigmodel.cn） | **直连**，不走代理。走代理反而会超时或连接失败 |
| 🌍 境外 API | **OpenAlex**、**Unpaywall**、**CrossRef**、**CORE**、**Zotero Web API** | **必须走代理** |

因此，你**必须开启 TUN 模式代理**（如 Clash Verge、v2rayN 等），让境外流量走代理、国内流量直连。

> **为什么必须 TUN？** 因为 MCP Server 跑在本地进程里（不经过浏览器），只有 TUN 模式才能在系统层面接管所有网络请求。普通 HTTP 代理对命令行程序不生效。

**Zotero LLM Wiki 已内置 `network_helper.py`**，会自动将 MinerU（mineru.net）的流量绕过 TUN 走直连。但你需要确保：

1. **代理软件 TUN 模式已开启**（不是"建议"，是"必须"，否则境外 API 全部超时）
2. 代理规则中 OpenAlex/Unpaywall/CrossRef 等域名走代理节点
3. MinerU 流量会被 `network_helper.py` 自动处理，无需手动配置绕过规则

## 项目结构

```
zotero-llm-wiki/
├── mcp_server.py          # MCP Server（11 个工具）
├── zotero_sync.py         # Zotero Web API：文件夹、创建、元数据
├── paper_discovery.py     # 学术搜索：OpenAlex/arXiv/CrossRef/Semantic Scholar
├── paper_importer.py      # PDF 下载瀑布 + Zotero 导入 + PDF 归档
├── pdf_parser.py          # MinerU Cloud API（PDF → Markdown）
├── chunker.py             # 文本切块（按章节切 500-1500 字块）
├── embedder.py            # 智谱 / Ollama 向量化
├── vector_store.py        # ChromaDB 向量存储 + 搜索
├── network_helper.py      # TUN 绕过（MinerU 国内流量直连）
├── config.py              # 配置加载（.env → Python）
├── run_ingest.py          # 批量入库脚本
├── scripts/
│   ├── zotero_obsidian_wiki.py       # Zotero LLM Wiki → Obsidian LLM Wiki
│   ├── zotero_obsidian_wiki_lint.py  # Wiki 健康检查
│   ├── zotero_obsidian_review.py     # 每日复盘笔记
│   ├── zotero_ingest_audit.py        # 入库审计报告
│   └── zotero_parsed_duplicate_report.py # parsed 重复缓存诊断
├── templates/
│   └── wiki/AGENTS.md     # LLM Wiki 维护规则模板
│
├── data/
│   ├── chroma_db/         # ChromaDB 向量数据库
│   ├── papers/            # PDF 永久存储（linked_file 指向这里）
│   ├── downloads/         # PDF 临时下载目录
│   ├── collection_map.json
│   └── parse_failures.json # 解析失败登记，用于避免重复消耗 API 额度
│
├── parsed/                # MinerU 解析缓存（每篇论文一个子目录）
│   └── {key}/
│       ├── {key}.md       # 解析后的 Markdown
│       └── images/        # 论文中的图片
│
├── .env                   # API 密钥（不提交 git）
├── .env.example           # API 配置模板
├── .gitignore
└── requirements.txt
```

## PDF 生命周期

一篇论文从下载到入库，PDF 全程只存 **1 份**：

```
download_paper(doi)
  → data/downloads/（临时文件）
      ↓
import_to_zotero()
  → 移到 data/papers/（永久存储）
  → Zotero linked_file 指向这里
      ↓
ingest_paper()
  → 从 data/papers/ 直接读取 PDF（不复制）
  → MinerU 解析结果缓存到 parsed/{key}/{key}.md + images/
      ↓
后续所有操作
  → 读 parsed/{key}/{key}.md 缓存，不碰 PDF
```

## MCP Server 工具清单（11 个）

### 搜索
| 工具 | 用途 |
|---|---|
| `search_papers` | 语义搜索文献库（支持指定文件夹、锁定特定论文） |
| `discover_papers` | 搜学术数据库（OpenAlex/arXiv/CrossRef/Semantic Scholar） |

### 下载 + 导入 + 入库
| 工具 | 用途 |
|---|---|
| `download_paper` | 6 级瀑布下载 PDF，不碰 Zotero 不碰向量库 |
| `import_to_zotero` | 导入 PDF + 元数据到 Zotero（linked_file 附件） |
| `ingest_paper` | PDF → OCR → 切块 → 向量化入库 |

### 文件夹管理
| 工具 | 用途 |
|---|---|
| `list_collections` | Zotero 文件夹 + 向量库 + 同步状态 |
| `create_collection` | 同时创建 Zotero 文件夹 + 向量库 |

### 引用
| 工具 | 用途 |
|---|---|
| `get_bibtex` | 精确引用 + 语义推荐（辅助写作） |

### 深度阅读
| 工具 | 用途 |
|---|---|
| `get_paper_chunks` | 论文结构目录（了解论文长什么样） |
| `expand_context` | 扩展上下文（深入读特定段落） |
| `read_paper_full` | 读全文 |

## 常见问题

**Q：我的论文已经有 PDF 了，还需要重新下载吗？**
不需要。`ingest_paper` 支持直接传 `pdf_path` 参数，跳过下载步骤。

**Q：向量库和 Zotero 不同步了怎么办？**
`list_collections` 会显示哪些文件夹已同步、哪些没有。用 `create_collection` 创建缺失的映射，然后对未同步的论文跑 `ingest_paper`。

**Q：MinerU 解析失败怎么办？**
MinerU 是**国内服务**（mineru.net），必须直连、不能走代理。`network_helper.py` 会在 TUN 模式下自动将 MinerU 流量绕过代理走直连。如果仍然失败，检查你的 TUN 代理规则是否误拦截了 mineru.net。也可以手动在 `parsed/{key}/` 下放置同名 `.md` 文件跳过解析。

如果某个 PDF 多次返回空结果或没有有效 chunk，它会进入 `data/parse_failures.json`。达到 `PARSE_FAILURE_MAX_ATTEMPTS` 后，增量入库会跳过它，避免反复调用 MinerU/embedding。确认问题已修复后，可以删除该文件中的对应 Zotero key，或临时设置 `PARSE_FAILURE_MAX_ATTEMPTS=0` 强制重试。

**Q：我想只搜索不入库，可以吗？**
可以。`discover_papers` 只搜索不入库。`download_paper` 只下载不入库。每一步都是独立的。

**Q：支持中文论文吗？**
MinerU 支持中文 OCR，智谱 Embedding 支持中文。中文论文可以正常解析和搜索。

## 技术栈

| 组件 | 用什么 | 说明 |
|---|---|---|
| Zotero 读写 | pyzotero + Zotero Web API v3 | 免费，需 User ID + API Key |
| PDF 解析 | MinerU Cloud API（VLM 模型） | REST API，不占本地 GPU |
| 文本向量化 | 智谱 Embedding-3 / Ollama local embeddings | 云端 API 或本地模型 |
| 向量数据库 | ChromaDB（本地持久化） | 纯文件存储，无需服务器 |
| Agent 接口 | MCP（Model Context Protocol） | stdio 通信 |

## 致谢

本项目的 Obsidian LLM Wiki 层受到 Andrej Karpathy 的 [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 理念启发：让 LLM 面向长期积累的 Markdown 知识库工作，使知识可以被持续整理、查询和复用。

在此感谢 Karpathy 对 LLM Wiki 模式的分享。本项目在这个思路上叠加了 Zotero 文献层、PDF 解析、向量检索、Obsidian 复盘/审计和可选本地 Ollama embedding，面向科研文献库的长期维护与对话。

## License

MIT License. 欢迎使用和贡献。
