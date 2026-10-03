# M5 完整备份恢复演练

状态：完整数据库/Vault 备份在同机隔离位置恢复，真实 API、Web 和新问答验收通过。本节点交付离线运维流程；没有新增 Web 备份按钮，也不表示完整损坏工作区重建或 M5/M6 完成。

操作说明见 [备份与恢复](../backup-and-restore.md)。

## 环境与恢复边界

- macOS，本机 PostgreSQL 16.14、pgvector 0.8.1、嵌入 LightRAG 1.5.7。
- 应用代码基线 `089bb51`，应用迁移头 `0012_source_file_operations`。
- 本机 Ollama：`qwen3.6:35b`、`qwen3-embedding:0.6b`，1024 维；workspace 保持 `knowgrain_setup_acceptance`。
- 原验收 API 在8787继续运行；恢复副本使用8788。两者使用不同应用库、不同 RAG 库和不同 Vault。各进程仅持有一个同事件循环的 Core 实例。
- 恢复目标为预先确认不存在后创建的 `knowgrain_restore_20261003`、`lightrag_restore_20261003`，同一隔离 PG 实例端口55433。
- Vault 恢复到新的空目录 `/private/tmp/knowgrain-restore-20261003.ne1UOg/Research`。使用已有模型和 tokenizer；没有声称新机器安装验收。

## 备份与实际恢复

备份取自上一节点在 API 正常关闭后的同一停写窗口，合成 Vault 没有外部编辑器写入：

```text
/tmp/knowgrain-m5-acceptance.TBHydm/pre-reconcile-app.dump
/tmp/knowgrain-m5-acceptance.TBHydm/pre-reconcile-rag.dump
/tmp/knowgrain-m5-acceptance.TBHydm/pre-reconcile-vault.tgz
```

1. 使用本机对应版本客户端 `createdb -T template0 --owner=knowgrain` 创建上述两个新库。没有覆盖、清空或删除原实例数据库。
2. 对两份归档分别执行 `pg_restore --single-transaction --exit-on-error --no-owner --no-privileges --dbname=<新目标库>`，均退出0。pgvector 已在目标服务器安装，RAG 归档中的扩展恢复成功。
3. 将可信自建 Vault 归档解压到新的空目录，保留 Sources、Wiki、Trash、`.knowgrain`、`.obsidian` 和其他原有文件。全部十八个保留文件安全读取后的 SHA-256 与备份基线一致；归档原件使用其准确 Trash 对应位置核对。
4. 在新应用库离线执行绑定迁移：同一事务连接断言 `current_database()` 为 `knowgrain_restore_20261003`，以备份中的实际旧规范路径为条件，参数化更新 `vault_binding` 的 `root_path`、新绑定 UUID 与 `updated_at`；`RETURNING` 必须恰好一行才提交。没有改动来源、修订、证据、Wiki 身份、审阅记录或 `created_at`。
5. 使用新的应用库/RAG 库、Vault 根/父目录和端口启动恢复 API。恢复实例使用新的空 `rag-work`，由 PostgreSQL 存储初始化；本次演练没有依赖旧 working directory 内容。常规备份仍应保存 working directory 和配置元数据，以覆盖不同运行状态。

恢复前后应用数据库包含：2个来源、3个修订、3个索引任务、5个 Wiki 页面、3个生成任务、2个审阅操作、1个页面生成绑定、1个证据记录、3个问答任务和3个文件操作。已有内容映射计数13保持不变。

## API 与 Web 验收

- 实际 `GET /api/v1/health/ready` 返回200，各依赖 ready；启动对账 complete，checked=1、healthy=1、repair_queued=0。
- 活跃来源的生命周期仍为6，latest/current 均为修订 `56ffa83e-99f2-48dd-87cf-e888070aaf87`，状态 ready；已删除来源及其归档文件仍保留。
- Wiki 实体接口返回 `binding_current=true`、`evidence_current=true` 和4个实体。审阅 Wiki SHA 保持 `e9bc26be36e0752123ab7534d115cd336042d9590d8a97d3a40af40202e6897d`。
- Web 真实问答页面显示三条恢复历史；打开旧有依据答案和引用，显示准确修订、原件哈希、字符位置与“与当前有效修订一致”。其中两条既有不足证据答案仍保留其原有结果，没有改写。
- 在恢复 Web 新建问题：“根据 Knowgrain 验收样例，Research Vault 保存什么？Web 在索引完成后应显示怎样的修订状态？请只回答原文直接支持的内容。”
- 新任务 `a7542ba0-7850-4229-a520-f687a303020d` 在一次尝试后 succeeded/answered，返回两条直接受原文支持的声明，引用当前修订。Core 日志记录实际检索4个实体、3条关系、1个向量文本块，再调用 query LLM；这不是仅展示恢复的缓存答案。
- Web 打开新答案引用，准确修订与原文位置正确；1280×720实际显示证据弹窗。截图仅在工具中观察，未导出图片文件。
- 三条既有问答的完整 API 响应与原8787实例逐项相等。新问答增加第四个问答任务；十八个保留文件及已审阅正文哈希不变。
- 恢复实例原件下载实际字节 SHA 为 `445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`，证据 Markdown SHA 与保留文件清单一致。原实例依然 ready，原件 HTTP 字节相等。
- 验收后正常关闭本任务创建的8788进程。日志确认模型队列关闭、十二个存储完成释放、PostgreSQL连接池关闭和应用 shutdown complete。保留新库与恢复文件供后续重建验收，关闭临时浏览器页；原8787实例继续运行。

本节点只修改文档，不重复无关全套测试；现有基线的354项后端检查、随后启动门控检查及24项前端检查见 [启动对账验收](m5-startup-reconciliation-2026-10-03.md)，不是本次又一次运行的结果。

操作文档的十个 shell 代码块分别通过 `zsh -n`，退出0且 stderr 为空；七个 Python heredoc 开闭匹配并全部编译通过，包括离线绑定 SQL 示例。根代理另用文档中的清单函数读取原 Vault 与恢复副本，三十七条文件和目录记录逐字一致。独立 Python 复审的临时夹具验证根/父符号链接、`..`、位于 Vault 内的备份输出被拒绝，恢复解包后再次校验新目录；含隐藏文件、空目录与子符号链接的 working 夹具经 tar 往返后清单一致。该夹具检查不扩大为有内容 working directory 的实际服务启动验收。数据库与通用文档审查通过，最终路径修复的 Python 复审通过。

## 证据文件

本地合成验收文件保存在 `/tmp/knowgrain-restore-20261003.ne1UOg/`：

- `restored-baseline.json`：恢复库行数、原绑定、十八个文件哈希。
- `relocated-binding.json`：离线绑定迁移结果。
- `api-restored.json`：恢复 API 就绪、来源、历史、实体和下载哈希。
- `new-query-observed.json`：新任务运行中观察记录。
- `restore-final-proof.json`：新答案、三条历史比较、全部保留哈希、Wiki、原件/证据下载哈希与两实例就绪结果。
- `runbook-check.json`：根代理最终十个 shell/七个 Python 示例检查和三十七条目录清单比较。
- `api.log`：恢复服务初始化、实际检索及请求日志，仅含合成验收资料。

这些临时证据未提交到 Git，不是持久备份产品。备份应存放在用户选定的长期位置。

## 范围与后续

本次证明同机完整备份可恢复到新的数据库与目录，并继续真实查询和读取准确证据。没有验证 Docker/PostgreSQL18、新机器、不同平台、物理断电、原生浏览器下载保存位置或 Obsidian 桌面客户端。

这依赖应用库与 RAG 库备份都完整。应用库丢失后的内容映射恢复、损坏 RAG 工作区从 Vault 全量重建、Embedding 改变后的显式重建、自动 Wiki 语义关系、完整图谱、第三方模型适配及 M6 仍为必需后续工作。新检索样例不是大规模语义正确率评估。
