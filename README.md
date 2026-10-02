# Knowgrain

本地优先的证据型知识 Wiki：Web 为操作界面，LightRAG Core 嵌入 Python 后端，Markdown Vault 保存原始资料和可由 Obsidian 打开的 Wiki。

## 当前可运行范围

当前已有 LightRAG CLI、常驻 FastAPI、资料上传/修订 API、Vault 原件存储、Markdown/TXT/PDF/DOCX 解析和 PostgreSQL 索引任务。`apps/web` 提供连接真实 API 的资料、Wiki 和问答页面：支持导入、上传新修订、状态查看、失败重试、Markdown 编辑、内部链接、反链、外部编辑冲突、有证据的 Wiki 生成、明确审阅和提案应用。问答保存逐条引用与来源修订，证据侧栏提供准确原件及 Markdown 摘录；Wiki 可通过引用文本块映射到 LightRAG 实体，并反查关联页面。恢复/发布和第三方模型配置仍在开发；`design/ui-concepts.html` 是早期界面草图。

资料层已通过单元测试和隔离 PostgreSQL 集成测试；真实本地 Ollama 与 pgvector 的 `ainsert → aquery_data`、Web 上传和修订索引已验证。此次使用隔离 PostgreSQL 16.14，Compose PostgreSQL 18 与远端 CI 尚未执行。证据见 [`本地验收记录`](docs/verification/m0-m1-local-2026-09-30.md)，完整里程碑状态见 [`docs/development-status.md`](docs/development-status.md)。

## 首次启动（macOS 开发环境）

需要 Python 3.12、`uv`、Docker Desktop（含 Compose）和 Ollama。先启动 Docker Desktop 与 Ollama 应用，再在仓库根目录执行：

```sh
make configure
make sync
make tokenizer
make db-up
make migrate
make models
make demo
```

`make demo` 会索引内置中文样例，并以 `mix` 模式输出实体、关系、文本块和来源引用的 JSON。首次安装需要下载 Python 依赖、两个模型和 tiktoken 词表；`make tokenizer` 下载并校验词表缓存。API 启动不会下载词表，缺失或校验失败时会提示重新运行该命令。

构建 Web 页面（需要 Node.js 22.12+ 和 npm）：

```sh
make web-sync
make web-build
make api
```

打开 `http://127.0.0.1:8787/`。已构建的 `apps/web/dist` 会由 FastAPI 在同一源提供；仅运行 API 时仍可用 `/docs`。前端开发可另开终端执行 `make web`，访问 `http://127.0.0.1:5173/`，Vite 将 API 请求代理给本机后端。构建前端后需要启动或重启 API，才能挂载新生成的目录。

启动常驻 API：

```sh
make api
```

API 默认只监听 `127.0.0.1:8787`。启动时先用带 3 秒连接/查询超时的 PostgreSQL `SELECT 1` 预检，再初始化 LightRAG；PostgreSQL 或 Ollama 尚未就绪时，进程仍会启动并可响应健康检查。`/api/v1/health/live` 返回 200，`/api/v1/health/ready` 实时检查 PostgreSQL 与 Ollama 中配置的两个模型，任一不可用时返回 503。启动和显式重试会实际调用一次 Embedding 模型并核对向量维度；常规就绪探测不会执行模型推理。修复外部服务后可调用 `POST /api/v1/system/retry-initialize` 重试；若仅 PostgreSQL 预检失败，重试不要求重启进程。若 LightRAG 存储初始化失败，API 会尽力逐个清理已创建的存储并标记必须重启进程；关闭期间存储 finalize 失败或被取消时也会保留实例句柄并标记必须重启。Swagger 页面位于 `http://127.0.0.1:8787/docs`。

可用以下命令检查健康状态：

```sh
curl -i http://127.0.0.1:8787/api/v1/health/live
curl -i http://127.0.0.1:8787/api/v1/health/ready
```

若只想先准备数据库，不下载模型：

```sh
make configure
make db-up
```

停止 PostgreSQL 容器但保留数据：

```sh
make db-down
```

PostgreSQL 数据放在 Docker named volume `knowgrain-postgres`。不要用 `docker compose down -v`，该命令会删除该卷中的本地数据。

Compose 使用 PostgreSQL 18，数据卷挂载到 `/var/lib/postgresql`，遵循[官方镜像的数据目录规则](https://github.com/docker-library/docs/blob/master/postgres/README.md#pgdata)。已有其他 PostgreSQL 版本的数据卷需要按官方升级流程处理，不能直接改镜像主版本。

## 导入资料与查看任务

先执行 `make migrate`（当前迁移为 `0010_m5_source_lifecycle`）。`.env` 中的 `VAULT_ROOT` 提供首次初始化位置，默认 `./data/vault`。启动 API 会创建 `Sources/Files/`、`Sources/Evidence/`、`Wiki/Drafts/` 和 `Wiki/Pages/`，保留已有文件及 `.obsidian`。可用 Obsidian 打开同一目录。

Web 顶部的 **Vault 设置** 可以预览和选择 `VAULT_PARENT_DIR` 下的一个文件夹（默认父目录 `./data/vaults`）。只输入文件夹名称，先查看哪些目录已存在、哪些将创建，再点击“使用此 Vault”。预览不写入文件；选择结果保存到应用数据库，重启时使用该绑定，后续改 `VAULT_ROOT` 不会覆盖选择。已有资料或 Wiki 后位置锁定，不能通过设置移动资料；后续迁移/恢复需专门流程。

首次接管旧资料数据库、重启以及重新选择同一目录时，服务会验证登记原件的路径和 SHA-256。原件缺失或变化时停用上传与索引，保留数据库和文件；恢复正确原件后使用“重连服务”。不要修改 `Sources/Files/` 中的原件来更新资料，请使用“上传新修订”。完整 Web 安装向导和第三方模型设置仍待实现。[Vault 验收记录](docs/verification/m1-vault-setup-2026-09-30.md)。

数据库迁移完成且 Vault 可写时，即使模型尚未就绪也可上传；索引任务停留在 `queued`。默认单文件上限 20 MiB，可用 `MAX_UPLOAD_BYTES` 调整，最高 100 MiB。

```sh
curl -F 'file=@example.md' http://127.0.0.1:8787/api/v1/sources
curl 'http://127.0.0.1:8787/api/v1/sources?limit=100&offset=0'
```

上传返回 `source_id`、`revision_id`、`job_id`、`duplicate` 和 Vault 相对路径。原件保存在 `Sources/Files/{source_id}/{revision_id}.{扩展名}`；原始文件名存入每个修订的元数据。相同内容去重；指定资料更新时生成新修订，旧原件保留。

列表默认每页 100 条，`limit` 范围 1–500，`offset` 从 0 开始。Web 中“导入资料”创建来源，“上传新修订”更新选中来源；检查器分别展示最近上传与当前索引的修订。模型未就绪时页面仍显示存储状态，允许保存原件并等待索引。

将下面的大写占位符替换成上传响应中的 UUID：

```sh
curl http://127.0.0.1:8787/api/v1/jobs/JOB_ID
curl -F 'file=@updated.md' http://127.0.0.1:8787/api/v1/sources/SOURCE_ID/revisions
curl -X POST http://127.0.0.1:8787/api/v1/sources/SOURCE_ID/reindex
```

任务状态为 `queued → running → succeeded/failed`。解析或模型失败会记录安全错误信息并保留原件；`reindex` 重试最新修订，已排队或运行中的任务返回 409。LightRAG 文档状态确认为 `processed` 后才将修订标为 `ready`。旧修订任务晚完成不会覆盖最新修订的当前指针。

相同修订与解析哈希重索引时保留 `indexed_at`，它表示该内容快照首次成功索引的时间；每次索引执行的完成时间记录在任务 `updated_at` 中。这样重建索引不会改变已有证据身份及摘录文件。

扫描 PDF、加密 PDF、无文本内容和无效 DOCX 会在解析阶段失败；当前不提供 OCR。Web 浏览器写请求必须同源，CLI 可直接调用本机 API。

词表缓存默认位于 `./data/tokenizers`，可用 `TOKENIZER_CACHE_DIR` 指定。安装时从[上游 tiktoken 使用的公开词表](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py)下载，按固定 SHA-256 校验。离线迁移时一并复制该缓存；安装命令下载软件资源，资料处理默认只调用本机 Ollama。

## Wiki 浏览与编辑

在资料页点击 **Wiki**，使用 **新建** 创建手动草稿。文件写入 `Wiki/Drafts/{kg_id}.md`，frontmatter 保存稳定的 `kg_id`、`kg_kind: wiki` 和 `kg_status`。编辑器可直接修改完整 Markdown，预览不显示 frontmatter。保存必须带读取时的 SHA-256；修改页面身份或状态会被拒绝，生成草稿的审阅使用下述明确审阅入口。

页面链接使用 `[[标题]]`、`[[路径#标题|显示文字]]` 或本页锚点；只有能唯一解析的页面才可点击跳转。反链显示来源页面、行号与锚点。代码、注释和转义链接不进入反链。外部编辑器改名或移动文件时保留 `kg_id`，并放在 `Wiki/Drafts/` 或 `Wiki/Pages/` 下，Web 会重新扫描定位；无身份、重复身份或损坏的页面显示扫描问题，扫描不会自动改写文件。

浏览器定期检查文件更新。编辑器有未保存内容时保留本地草稿；陈旧保存返回 HTTP 409，显示服务器内容和差异，可显式载入服务器版本。正常保存保留被替换版本到 `.knowgrain/wiki-recovery/{kg_id}/`。无法与不遵守应用锁的外部编辑器实现操作系统级原子版本比较：最后核验与替换之间仍有极小竞争窗口，详见 [M2 文件契约](docs/m2-wiki-contract.md)。数据库仅保存页面与链接投影，Markdown 是正文权威。

## Wiki 生成与审阅

已接入主题生成任务、严格声明与引用校验、当前原件核验和新草稿发布。生成任务先保留结果，重试沿用同一页面身份；人工改过的草稿不会被自动替换。模型未就绪时可排队，执行器等待 Core 就绪。

```sh
curl -H 'Content-Type: application/json' -d '{"topic":"根据已导入资料整理项目说明"}' http://127.0.0.1:8787/api/v1/wiki/drafts
curl http://127.0.0.1:8787/api/v1/wiki/generation-jobs
```

任务返回 `output_page_id`；生成后可通过 `GET /api/v1/wiki/pages/PAGE_ID/generation` 查看声明、证据和当前有效性，通过 `GET /api/v1/evidence/EVIDENCE_ID` 查看摘录。证据失效仍可查看，但不会标记为当前依据。HTTP 409 表示状态/内容冲突，503 表示数据库或 Vault 尚未就绪；失败任务可使用 `POST /api/v1/wiki/generation-jobs/JOB_ID/retry` 重试。

在 Wiki 页面输入主题，点击 **生成新草稿**；也可选中页面后点击 **针对当前页面生成提案**。任务完成后打开生成页，逐条核对声明与原始摘录。只有未修改的生成版本与当前有效证据才可审阅。点击 **明确标记为已审阅** 后，页面保存到 `Wiki/Pages/`；提案需核对目标差异并点击 **应用提案到目标页面**，原提案仍保留。人工未保存的编辑会阻止这些操作。

审阅 API 为 `POST /api/v1/wiki/pages/PAGE_ID/review`，请求 `{expected_sha256}`；应用提案为 `POST /api/v1/wiki/pages/PROPOSAL_ID/apply`，请求 `{expected_proposal_sha256, expected_target_sha256}`。页面、来源或目标发生变化返回 409；写入或数据库暂时故障返回 503。界面在网络/503 失败后保留原请求，提供 **继续上次操作**。若已重载浏览器，恢复时需复用原请求哈希：审阅使用清单中的 `generated_sha256`，提案使用其生成哈希和 `proposal_target_sha256`。服务端通过 `.knowgrain/review-operations/` 中的不可变日志恢复，只接受该操作的准确旧/新内容；不自动覆盖外部编辑。完整故障恢复界面仍属 M5 工作。

本地 `qwen3.6:35b` 已通过真实 Core 检索与 Wiki 草稿生成验证，见 [模型切换验证](docs/verification/qwen36-local-2026-10-01.md)。M3 的 Web 审阅、提案应用、冲突保护、同请求重试和审阅状态重启恢复已本地验收，详见 [审阅验收记录](docs/verification/m3-review-2026-10-02.md)。生成正文的引用校验不等同于事实语义核实，审阅需要将声明与原文并列核对；M5–M6 与第三方模型适配仍在路线图中。

## 有依据的问答

在资料页点击 **问答**，输入问题后点击 **查阅资料**。本机任务会先检索并验证当前资料，再用配置的模型生成答案。记录保存到 PostgreSQL，离开页面或刷新后可重新打开。每条声明旁的引用和 Wiki 中的 `Sources/Evidence/UUID` 链接都能打开原文侧栏，展示准确修订、完整哈希、可获得的位置和索引时间。

```sh
curl -H 'Content-Type: application/json' -d '{"question":"根据已导入资料，这个项目的原件保存在哪里？"}' http://127.0.0.1:8787/api/v1/queries
curl http://127.0.0.1:8787/api/v1/queries
curl http://127.0.0.1:8787/api/v1/queries/JOB_ID
```

POST 返回 202 和 `job_id`，不等待模型完成。失败任务可 `POST /api/v1/queries/JOB_ID/retry`；租约过期后执行器会重新领取未完成任务。无当前证据或模型判断资料不足时返回 **无法核实**，没有未引用的补充正文。历史答案重新读取时校验来源；过期证据有明确提示，不会进入新答案。

`GET /api/v1/evidence/EVIDENCE_ID/original` 返回经过完整 SHA-256 检查的对应原件字节；`/markdown` 返回与保留清单一致的 Vault 摘录。缺失文件返回 404、内容变化返回 409、不安全路径或服务故障返回 503。浏览器不能传服务器文件路径。详见 [M4 问答契约](docs/m4-question-contract.md)。引用校验不能代替语义准确率评估；M5–M6 工作尚未完成。

### Wiki / 实体双向导航

在 Wiki 页面底部打开“LightRAG 实体”，点击实体查看关联 Wiki，或直接打开原文证据。只有正文仍匹配当前生成/审阅绑定、全部引用来源仍有效的页面才显示映射；手动页面或已编辑正文不自动继承旧关联。有未保存编辑时隐藏该导航，保存后重新核对。

`GET /api/v1/wiki/pages/PAGE_ID/entities` 返回实体、绑定/证据有效状态和当前正文哈希；`GET /api/v1/graph/entity-pages?name=EXACT_NAME` 反查页面。实体 ID 为精确名称的 SHA-256；关联由准确修订与完整文本块成员证明，不按标题相似度猜测。页面/实体数量受限时返回 `truncated`，不可把有限列表当作完整集合。读取不调用模型，也不写入 Vault。详见 [接口契约](docs/m4-entity-mapping-contract.md)和[真实验收](docs/verification/m4-entities-2026-10-02.md)。

## 开发验证

```sh
make test
```

默认运行 Vault、解析器、上传大小边界和索引任务测试；真实 PostgreSQL 测试默认跳过。对专门创建的测试数据库执行迁移后，可显式启用集成测试：

```sh
KNOWGRAIN_POSTGRES_DB=knowgrain_test uv run alembic upgrade head
KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=5432 uv run python -m unittest discover -s tests -p test_sources_postgres.py -v
```

测试库名称必须以 `knowgrain_test` 开头。测试固定连接 `127.0.0.1`、用户 `knowgrain`，密码默认 `knowgrain-local`，可通过 `POSTGRES_PASSWORD` 环境变量覆盖；测试不加载 `.env`。集成测试仅清理自身创建的资料记录，不把真实 Vault 用作测试目录。这些测试使用真实 PostgreSQL 事务，但模型调用使用测试替身，不能替代 `make demo` 的模型/向量验收。

## 本地模型默认值

- 聊天/抽取：`qwen3.6:35b`（按当前本机选择）
- Embedding：`qwen3-embedding:0.6b`，维度 `1024`
- Ollama：`http://127.0.0.1:11434`

可在 `.env` 中调整模型和地址。Embedding 模型或维度改变后，需要清理并重建 LightRAG 索引；不要对已有资料只改维度设置。

## 第三方模型

架构预留 LLM 与 Embedding 独立 provider 配置。首个 M0 运行入口暂时只接 Ollama；第三方 provider 会在后续实现并按真实支持范围更新此文档。

## 开发阶段

总体目标和 M0–M6 验收标准见 [`docs/architecture-and-development-plan.md`](docs/architecture-and-development-plan.md)，依赖细节见 [`docs/dependencies-and-local-setup.md`](docs/dependencies-and-local-setup.md)。
