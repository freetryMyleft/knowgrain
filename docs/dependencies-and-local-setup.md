# Knowgrain 依赖清单与本地安装建议

状态：实施中（2026-10-03）。仓库已有锁文件、PostgreSQL Compose、LightRAG CLI、FastAPI、应用迁移、Vault 导入、四类解析器、索引任务，以及真实 API 驱动的资料/Wiki/问答界面。主题生成、明确审阅、提案应用、逐条问答引用和证据导航已实现；当前运行默认使用 Ollama `qwen3.6:35b` 与 `qwen3-embedding:0.6b`。真实 Core/上传、Vault 绑定、Wiki 冲突和 M3 审阅见 [开发状态](development-status.md)中的验收记录。此次使用隔离 PostgreSQL 16.14；Compose PostgreSQL 18 与远端 CI 尚未执行。Wiki/LightRAG 实体双向导航与完整备份的隔离恢复已验证；完整图谱可视化、安装向导、全量索引重建、发布及第三方模型配置仍未完成。本页区分当前运行步骤与后续依赖建议。

## 1. 推荐组合

首版推荐 **单个 Knowgrain Python 后端进程（内嵌 LightRAG Core 与作业执行器）+ PostgreSQL/pgvector + 本机 Ollama + 本机 Vault 文件夹**。Web 前端由后端提供静态构建产物；当前模型配置使用 Ollama，独立选择第三方 LLM/Embedding API 的适配层与设置页仍待开发。Obsidian 桌面应用只用于打开和编辑同一个 Vault，可不安装。

| 类别 | 推荐内容 | 安装位置 | 必需性与用途 |
| --- | --- | --- | --- |
| 容器运行 | Docker Desktop，内含 Docker Compose | 开发机/部署主机 | 推荐用于 PostgreSQL 和后端打包；已有兼容的 Docker Engine + Compose 时不用装 Desktop。开发时后端也可直接在宿主机运行。 |
| 数据库 | PostgreSQL + `pgvector` 扩展；Compose 采用 `pgvector/pgvector` 镜像并固定镜像版本/摘要 | 容器 | 正式本地方案必需。应用元数据和 LightRAG 索引分数据库/账号；LightRAG 向量索引需要 `vector` 扩展。 |
| 检索核心 | `lightrag-hku` Python 包，固定经过验证的版本 | Knowgrain 后端环境/镜像内 | 必需。与 API 运行在同一 Python 进程，通过异步 Core API 处理索引和检索。无需启动独立 LightRAG Server。 |
| 模型服务 | Ollama；第三方 LLM/Embedding API 可选 | Ollama 装在本机；第三方在外部 | 当前本地实现以 Ollama 为默认；LightRAG 需要同时配置 LLM 与 Embedding。未来可不安装 Ollama，改用第三方服务。 |
| 文件库 | 一个普通本机文件夹，例如 `~/Documents/KnowgrainVault` | 宿主机 | 必需。后端容器使用时绑定挂载；原件和已审阅 Markdown 存在这里。 |
| 知识编辑器 | Obsidian 桌面应用 | 宿主机 | 可选。打开上述 Vault，提供本地编辑和原生反链视图；Web 页面本身可完成主要操作。 |
| 开发工具 | Python 3.12、`uv`、Node.js 22.12+ 或 24 LTS、npm、Git | 开发机 | 仅源码开发需要；使用预构建容器部署时不用在宿主机安装 Python/Node。Vite 当前要求 Node 20.19+ 或 22.12+。 |

**建议先不装：**Neo4j、Apache AGE、Redis、Qdrant/Milvus、Electron。当前选型使用 PostgreSQL 的四类 LightRAG 存储，作业队列也放在 PostgreSQL；增加这些组件会扩大安装和运维面，首版没有对应收益。若以后资料规模或并发需求超过单机方案，再按测量结果替换。

| 本地运行方式 | 需要在宿主机安装 | 说明 |
| --- | --- | --- |
| 开发推荐 | Python + uv、Node.js + npm、Ollama、Docker Desktop（仅运行 PostgreSQL） | 后端与 Vite 直接在本机运行，修改代码可热更新；LightRAG Core 仍在后端进程内。 |
| 本地打包版 | Docker Desktop、Ollama | Compose 启动 Knowgrain 后端和 PostgreSQL，后端提供已构建 Web；Vault 是宿主机目录。 |
| 无 Docker 纯宿主机 | Python + uv、Node.js + npm、PostgreSQL + pgvector、Ollama | 可以实现，但要自行安装/启动数据库并维护服务；适合明确不使用 Docker 的环境。 |

三种方式的数据都留在本机 Vault 与本机 PostgreSQL。只有主动选择第三方模型时，模型请求才会离开本机。

## 2. 应用代码需要的库

下表是未来项目的**直接依赖建议**。依赖版本应在首次实现时锁进 `uv.lock`、`package-lock.json`，不要长期使用浮动的 `latest`。`lightrag-hku` 由 Knowgrain 后端直接安装；解析 PDF/DOCX 也由本项目负责，因此列出对应库。

| 模块 | 建议安装的库 | 用途 / 选择理由 |
| --- | --- | --- |
| Python API | `fastapi`、`uvicorn[standard]`、`pydantic-settings`、`python-multipart` | HTTP 接口、运行服务、环境配置、上传文件。 |
| 嵌入式检索 | `lightrag-hku` | 在后端进程持有一个 `LightRAG` 实例；调用 `ainsert`、`aquery_data` 等异步 Core 方法。固定版本并由适配层隔离返回格式。 |
| Python 后端 | `sqlalchemy[asyncio]`、`asyncpg`、`pgvector`、`alembic` | 异步访问 PostgreSQL；`asyncpg` 和 Python `pgvector` 包也满足 LightRAG PostgreSQL 存储运行依赖；`alembic` 管理 Knowgrain 自己的表。数据库内的 `vector` 扩展仍需单独启用。 |
| 文件解析 | `pypdf`、`python-docx` | 将 PDF、DOCX 转为文本后交给 LightRAG Core；Markdown/TXT 用标准库读取。扫描版 PDF 的 OCR 后续单独引入。 |
| 模型连接 | `ollama`、`httpx`；第三方 OpenAI 兼容模型启用时增加 `openai` | LightRAG 的 Ollama 模型函数依赖 Python `ollama` 客户端；`httpx` 做健康检查；第三方模型用上游 OpenAI 兼容适配函数。 |
| Vault 适配器 | `pyyaml`、`watchfiles`（已安装） | 有界 SafeLoader 读取 YAML frontmatter；保存已有文档时保留原文。事件监听加定期扫描发现外部 Markdown 修改。`[[wikilink]]` 解析和安全路径校验由项目实现。未引入额外的 `ruamel.yaml`。 |
| Python 开发/测试 | `pytest`、`pytest-asyncio`、`ruff` | 测试异步作业和 API；格式与静态检查。 |
| Web 基础 | `react`、`react-dom`、`typescript`、`vite`、`@vitejs/plugin-react` | 构建单页 Web 应用；不需要 Next.js 服务端渲染。 |
| Web 状态/路由 | `react-router-dom`、`@tanstack/react-query` | 页面导航；请求、缓存和重试。 |
| Markdown 编辑/预览 | `@codemirror/state`、`@codemirror/view`、`@codemirror/commands`、`@codemirror/lang-markdown`、`react-markdown`、`remark-gfm`（已安装） | CodeMirror 6 编辑器和 GFM 预览。React 组件直接管理编辑器生命周期，无需额外包装库。预览不执行原始 HTML，不加载外部图片；`[[链接]]` 和块 ID 由应用转换。未来若启用原始 HTML，必须加入明确的清理规则。 |
| 知识地图 | `cytoscape` | 实体、关系图的渲染与交互；首版先限制返回节点数，避免大图卡顿。 |
| Web 开发/测试 | `@types/react`、`@types/react-dom`、`vitest`、`@testing-library/react`、`@testing-library/jest-dom` | TypeScript 类型与关键交互测试。 |

初期无需另装 LightRAG Server、Neo4j 客户端或其他向量数据库客户端。`lightrag-hku`、`asyncpg`、Python `pgvector` 包、Python `ollama` 包、`pypdf` 和 `python-docx` 是本地后端的直接依赖。不要为了这两个后端把上游 `offline-storage`/`offline-llm` 全套可选依赖都装进首版；其中还包含本项目用不到的数据库与模型客户端。

当前根目录 `pyproject.toml` 已声明 Python 运行依赖，`uv.lock` 已生成。源码开发者在仓库根目录安装锁定依赖：

```sh
uv sync --locked
```

已安装依赖以 `pyproject.toml`/`uv.lock` 和 `apps/web/package.json`/`package-lock.json` 为准。M2 已加入 PyYAML、watchfiles、上述 CodeMirror 与 Markdown 预览库；路由/状态框架、图谱库及 pytest/ruff 仍为后续建议。后端使用标准库 `unittest`；前端契约检查使用 Node 内置测试运行器。只在相应模块实现时加入其直接依赖。

## 3. 两种模型配置

| 方案 | 需要安装 | 推荐起点 | 适用场景与注意点 |
| --- | --- | --- | --- |
| 完全本地 | Ollama；下载一个聊天/抽取模型和一个 Embedding 模型 | 当前本机选择 `qwen3.6:35b` + `qwen3-embedding:0.6b` | 不向云端发送资料；模型运行取决于机器内存和算力，需用样例资料评估抽取与生成质量。 |
| 云端/混合 | 无需本地模型运行时；配置提供方密钥与模型 | 一个支持 LightRAG 的 LLM 提供方 + 一个 Embedding 提供方 | 本机要求较低；资料/问题可能发送给所选提供方。密钥只保存在服务端配置，不写入 Web 构建产物。 |

本地方案的模型下载示例（**装好 Ollama 后**）：

```sh
ollama pull qwen3.6:35b
ollama pull qwen3-embedding:0.6b
ollama list
```

当前运行配置使用本机 Ollama，默认 `LLM_MODEL=qwen3.6:35b`、`EMBEDDING_MODEL=qwen3-embedding:0.6b`；后端据此构造 LightRAG Core 的 `llm_model_func` 与 `EmbeddingFunc`。已有 `.env` 或启动环境中的 `LLM_MODEL` 会覆盖默认值，切换后需重启后端。LLM 单独切换不要求重建向量；历史索引中的实体摘要不会自动重新抽取，若需新模型重抽取，使用资料重索引流程。后端直接在 macOS 宿主机运行时用 `http://localhost:11434`；若后端在 Docker 容器内而 Ollama 在宿主机，容器中通常要用 `http://host.docker.internal:11434`。安装向导须检查从**后端运行环境**到模型服务的连通性、真实输出向量维度、上下文窗口和一次最小推理。`embedding_dim` 必须与实际模型输出匹配；确定模型和维度后再导入正式资料。更换 Embedding 模型或维度需要重建 LightRAG 向量数据并重新索引，不能只改配置。

## 4. 开发机安装顺序

**只体验未来发布的容器版：**安装 Docker Desktop 和 Ollama，准备 Vault 目录；到 M0 实现 Compose 后，由项目脚本启动后端与 PostgreSQL。用户不必单独装 Python 包、Node 包或 Obsidian。若明确配置第三方 LLM 与 Embedding，才可以不安装 Ollama。

**参与开发（推荐方式）：**

1. 安装 Git、Docker Desktop、Python 3.12、`uv`、Node.js 22.12+（或 24 LTS）、Ollama。在 macOS 上可用各工具官方安装包；Obsidian 按需要安装。
2. 用 `docker version`、`docker compose version`、`python3 --version`、`uv --version`、`node --version`、`npm --version` 检查工具可用。
3. 在仓库根目录执行 `uv sync --locked` 安装当前 Python 依赖。
4. 执行 `make configure`、启动 Docker Desktop，然后运行 `make db-up` 创建应用数据库与 LightRAG 数据库；`vector` 扩展由初始化 SQL 在 LightRAG 数据库启用。执行 `make migrate` 显式迁移到 `0012_source_file_operations`；API 启动不会自动建表或迁移旧库。先备份已有数据库与 Vault，再升级。0011 为已删除来源建立持久化清理任务，启动后会通过内嵌 Core 清理派生索引；原件和已审阅 Wiki 保留。0012 记录全部修订的归档/恢复意图；清理完成后原件进入同一 Vault 的 `Trash/Files`，恢复时核验并搬回；历史原件下载支持确切归档路径。文件任务仅需应用库/Vault，不依赖模型就绪。
5. 在 `.env` 设置 `VAULT_ROOT`（默认 `./data/vault`），启动 Ollama 并执行 `make models` 下载示例模型，再运行 `make demo` 验证 LightRAG Core 的最小导入与结构化检索。`make api` 启动常驻进程；使用 `/docs` 或 README 的 curl 上传资料、查看修订/任务和重试。手动 Wiki 操作仅依赖应用数据库和 Vault；生成和问答需要本机模型联通。独立模型协议适配的内部基础已有本机验收，复用现有 httpx 和固定版 Core 的 NumPy，未增加依赖；应用第三方模型切换仍待代账本/配置匹配接入，见 [适配层边界](provider-role-adapters.md)。

当前可执行的开发启动命令如下；`docker compose` 要求 Docker Desktop 已启动，Ollama 也必须已启动：

```sh
make configure
uv sync --locked
make db-up
make migrate
make models
make tokenizer
make demo
```

另开一个终端运行 `make api`（执行 `uv run knowgrain-api`）。API 默认只监听本机 `127.0.0.1:8787`。启动前会用带 3 秒连接/查询超时的 PostgreSQL `SELECT 1` 做预检；数据库不可用时，API 进程仍会启动并暴露存活与就绪检查，就绪状态为 503。数据库恢复后可调用 `POST /api/v1/system/retry-initialize` 重试；这个预检失败发生在 LightRAG 存储初始化之前，不要求重启进程。存储初始化本身失败时会尽力清理已建存储并标记必须重启，因为不能确认 LightRAG 内部连接池已完全释放。

模型任务启动前，会验证已登记原件的哈希及来源/当期归档日志的位置，然后对账当前 ready 修订的 Core 文档记录。已确认缺失的文档会自动排队重索引；对账读失败不会视为数据不存在，需检查数据库/Vault并用“重连服务”重试。文件日志重放和 Wiki 扫描保持独立运行。服务卡或 `GET /api/v1/system/reconciliation` 显示最近一次检查，修复进度仍看来源索引任务。完整工作区重建尚未完成。完整数据库与 Vault 备份已有同机隔离恢复演练，离线步骤见 [备份与恢复](backup-and-restore.md)，实际证据见 [恢复验收](verification/m5-backup-restore-2026-10-03.md)；这不等于新机器安装或 Docker 验收。

旧安装升级时核对实际工作区：固定版LightRAG读取 `POSTGRES_WORKSPACE`（日志称为 `PG_WORKSPACE`），也可能读取 `config.ini` 的 `[postgres] workspace`。它们与 `LIGHTRAG_WORKSPACE` 不同时，新默认运行时明确拒绝启动Core；先将配置同步到原实例实际使用的 namespace，再重启 API。Settings 与默认索引身份在应用构造时固定，“重连服务”不会重新读取 `.env`。不要更改工作区名来绕过故障。现有默认入口仍保留旧无后缀向量表，内部显式目标身份已验证；[身份验收](verification/m5-core-identity-2026-10-03.md) 不代表全量重建按钮已上线。

健康检查：

```sh
curl -i http://127.0.0.1:8787/api/v1/health/live
curl -i http://127.0.0.1:8787/api/v1/health/ready
```

`/api/v1/health/live` 在服务进程正常响应时返回 200；`/api/v1/health/ready` 检查 LightRAG、两个 PostgreSQL 数据库、Ollama 模型与 Vault 初始化状态，依赖不全时返回 503。启动与显式重试会调用一次 Embedding 模型并检查实测向量维度，常规就绪探测不会执行模型推理。Swagger 页面位于 `http://127.0.0.1:8787/docs`。应用数据库迁移完成且 Vault 可写时，模型缺失不会阻止原件上传，任务等待模型就绪后处理。

Web 安装和构建：`make web-sync`、`make web-build`，随后 `make api`；在 `http://127.0.0.1:8787/` 打开资料页面，点击 **Wiki** 浏览和编辑 Markdown。开发时可另开终端 `make web` 使用 Vite。页面直接连接真实资料/任务/Wiki API；已有问答、证据、生成、审阅和更新提案。完整图谱、恢复重建工具、安装向导和第三方模型设置仍待实现。当前 Compose 只运行 PostgreSQL，完整产品发布镜像仍待 M6。

LightRAG 的 tiktoken 词表也需要首次联网准备：`make tokenizer` 下载固定词表并校验 SHA-256，默认缓存目录 `./data/tokenizers`。API 启动只检查本地缓存，缺失时不会隐式下载并阻塞事件循环。模型、Python/npm 依赖和词表都准备好后，本地资料处理可断网运行；离线迁移需复制这些运行资源。

首个端到端检查应验证：上传一份 Markdown → Vault 原件出现 → LightRAG Core 索引成功 → 问答显示可打开的原文证据 → 生成草稿 → Obsidian 可打开同一 Markdown。仅检查进程为 `running` 并不足以证明引用链正确。

## 5. 本地安装向导应提供的检查

未来 Web 的“首次设置”页面建议按顺序显示：

1. **环境**：数据库健康、磁盘可写和空间、应用与嵌入式 LightRAG 包版本是否匹配。
2. **Vault**：选择或创建目录，测试读写、检查已有 `Wiki/` 和 `Sources/` 文件、对已有文件先做只读扫描；绝不静默覆盖 Obsidian 文件。
3. **模型**：选择 Ollama 或云端；分别检查 LLM 与 Embedding；展示模型名称、实测向量维度和是否会向外部发送内容。
4. **连接**：检查进程内 LightRAG Core 初始化是否成功；验证 PostgreSQL 扩展和四种存储配置；不检查不存在的独立 LightRAG HTTP 服务。
5. **完成**：创建样例页面或导入示例资料，展示首个来源到 Wiki 的可追溯链路。

配置模板应分为公开设置与服务端机密；Web 只能收到不含密钥的状态。Vault 和数据库都要纳入备份。安装器执行任何数据库初始化、已有 Vault 修改前，先展示目标路径和将创建的内容。

## 参考依据

- [LightRAG 项目与 Core 用法](https://github.com/HKUDS/LightRAG)
- [LightRAG Core 编程接口](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)
- [LightRAG API Server：模型和四类 PostgreSQL 存储](https://github.com/HKUDS/LightRAG/blob/main/docs/LightRAG-API-Server.md)
- [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/) 与 [Compose 安装说明](https://docs.docker.com/compose/install/)
- [Vite 环境要求](https://vite.dev/guide/)
- [Ollama Qwen3 模型](https://ollama.com/library/qwen3) 与 [Qwen3 Embedding 模型](https://ollama.com/library/qwen3-embedding)
- [Obsidian 文件存储](https://obsidian.md/help/data-storage)
