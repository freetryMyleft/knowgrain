# M5 当前修订启动对账与自动修复

状态：本节点本地验收通过。不是完整工作区重建、备份恢复或 M5/M6 验收。

## 实现

- 同一事件循环与 Core 写锁下严格读取文档状态、完整文本、实体/关系文档清单、已知所属块，区分正常、确定缺失与不一致。存储读取失败保持未知，不误判为不存在。
- 按来源 UUID 游标分页，检查 active、latest=current、ready、任务 succeeded 的来源；使用原有任务表排队 cleanup → insert。生命周期、修订、原件/解析哈希、索引时间、任务更新时间均参与 CAS。排队时清空 current，保留不可变内容快照。
- Vault 验证来源的全部修订目录与当期文件日志；active 不允许原件仅位于 Trash；当前归档成功需要 Trash，未完成意图允许唯一的 Sources/Trash 位置。双位置、额外成员、缺失或改动均拒绝启动。
- 启动/重连在对账成功后才启动索引、清理、生成和问答执行器；Wiki 扫描与文件任务不依赖模型。取消 Vault 读取时排空线程后释放锁。
- Web 服务卡显示本进程对账次数及修复排队数；`GET /api/v1/system/reconciliation` 提供同一报告。报告完成不表示所有修复任务已完成，任务仍由原有持久索引队列管理。

## 自动检查与审查

- `KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 .venv/bin/python -m unittest discover -s tests -q`：354 项通过，19.788 秒，实际数据库为明确选择的可丢弃夹具。日志 `/tmp/knowgrain-m5-reconciliation-tests.log`。
- 之后新增一项启动失败门控测试，`python -m unittest tests.test_application_runtime -q` 四项通过：对账失败时模型任务未启动，文件恢复及 Wiki 仍可运行。本记录不把这四项说成又一次全套运行。
- 对账 PostgreSQL 文件九项通过（六项事务、三项输入校验）。涵盖游标、持久清单合并、重复排队、来源导入/删除/重试/任务更新时间变化、文件任务排除、缺失解析元数据及当期位置日志。
- Core 检查十四项、原有清理二十一项通过；对账服务六项、Vault 十八项通过。
- `npm run build`、TypeScript 和二十四项 Node 契约检查通过。Wiki chunk 大小提示仍待 M6；没有添加依赖或迁移，应用 schema 仍为0012。
- Python、数据库、TypeScript 和综合有限审查通过。审查中核对实际上游索引路径，普通 `ainsert` 的位置块 ID 不能使用内容 MD5 规则验证；修复空块清单和构造函数类型。

## 真实 Core 缺失与 API 重启

使用已存在的隔离合成来源 `2b10760a-78b5-48e8-8a58-036854c1a920`、当前修订 `56ffa83e-99f2-48dd-87cf-e888070aaf87`。模型为本机 qwen3.6:35b、qwen3-embedding:0.6b/1024维，嵌入 LightRAG 1.5.7，PostgreSQL 16.14/pgvector。

1. 正常关闭旧 API，保存应用/RAG dump 与 Vault 压缩包。全部十八个保留文件安全读取后哈希一致，原有当前文档检查为 healthy。
2. 独立 Core 进程通过公共 `adelete_by_doc_id` 删除目标派生索引并保留清理清单，正确释放十二个存储。应用库保持原 ready/current 记录，Core 检查实际为 missing。没有删除权威文件或手工删除数据库图谱行。
3. 启动新 API：对账 checked=1、healthy=0、repair_queued=1、complete，自动索引最终 ready/current=latest、任务 succeeded。第一次 HTTP 观察已完成重索引，因此没有声称在浏览器观察到此次修复的 current=null 瞬间；该状态由真实事务测试验证。
4. 来源生命周期仍为6，内容首次 indexed_at 保持原值；十八个原件、证据、Wiki 和保留日志文件全部 SHA 不变。原件实际 HTTP 下载哈希一致，Wiki 实体接口 binding_current/evidence_current均true，返回四个实体。
5. Web 实际显示“检查1、正常0、修复已排队1”。点击“重连服务”后新报告变为“检查1、正常1、修复已排队0”；原任务 attempts/updated_at 未变化，没有重复重建。

证据目录 `/tmp/knowgrain-m5-acceptance.TBHydm/`：

- `pre-reconcile-app.dump`、`pre-reconcile-rag.dump`、`pre-reconcile-vault.tgz`。
- `reconciliation-core-inspections.json`、`reconciliation-baseline.json`、`reconciliation-missing-before-restart.json`。
- `reconciliation-restart-observed.json`、`reconciliation-restored-final.json`、`reconciliation-evidence-restored.json`、`reconciliation-repeated-healthy.json`。
- `reconciliation-injection.log` 与 `reconciliation-api.log`。

Web 在正常760×664窗口验收；服务区可滚动查看完整报告，没有新增布局溢出。截图仅在工具中观察，未导出图片文件。没有操作 Obsidian 桌面客户端。

## 范围与后续

healthy 仅证明完整文档哈希和已知块结构/身份有效，不证明每块正文逐字一致、全部向量与图谱行完整，也不发现所有未知孤立 Core 记录。上游安全删除无法清理的部分损坏仍会明确失败。重索引可复用保留的模型缓存，不宣称每次都触发新的 LLM 推理。

来源原件位置核对覆盖已登记的全部修订；未登记孤立文件和应用数据库丢失后的内容映射恢复仍待实现。完整损坏工作区重建、备份恢复演练、自动 Wiki 语义关联、完整图谱、第三方服务适配和 M6 继续保留为必需工作。
