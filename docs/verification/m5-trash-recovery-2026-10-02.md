# M5 Trash 归档与文件恢复验收

状态：本节点本地验收通过；完整 M5/M6 未完成。

## 实现与迁移

- `0012_source_file_operations` 增加全部历史修订的固定文件清单和归档/恢复日志，来源/周期/类型唯一，每来源最多一项 queued/running。最后一个当期 Core 清理成功时原子排队归档；已清理 deleted 来源由迁移回填。
- 整个来源目录在 `Sources/Files/{source_id}` 与 `Trash/Files/{source_id}` 之间移动。平台独占 rename 不替换目标；多余成员、缺失、变化、符号链接与非普通文件均拒绝。移动与重放同步父目录，哈希读取有大小上限。
- 恢复先持久化意图、核验并搬回全部修订，再同事务完成日志/激活来源/排队重索引。文件写锁覆盖领取、移动、核验、完成与 owner 释放；取消时等待线程结束。后台文件恢复只依赖应用库与 Vault。
- 历史原件读取保留 canonical 身份，仅在规范原件路径 missing 时访问唯一 Trash 对应位置。新索引及没有文件恢复服务的旧入口禁止此回退。
- Web 接入状态、错误、失败重试及 busy 协调。恢复尚未完成的当前意图会阻止相反方向操作；旧周期不能完成新任务。

## 自动化与审查

- `KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 .venv/bin/python -m unittest discover -s tests -q`：323 项通过，18.896 秒；日志 `/tmp/knowgrain-m5-trash-tests.log`。数据库是本任务可丢弃夹具，没有使用默认库或验收库运行测试清理。
- 新文件日志 PostgreSQL 套件4项、Core 维护5项、来源14项均通过。覆盖全部修订清理前不归档、同周期恢复阻止清理、重复删除周期、租约过期接管、Source 行锁等待期间过期拒绝完成，以及有已排队/未清理前缀的有界扫描。没有声称每层行锁分别等待都已测试。
- 文件层10项通过，包含真实平台独占 rename 碰撞、移动后 fsync 失败重试，以及新目录 fsync 失败时全部 FD 关闭。执行器8项包含实际文件与仓储替身的移动后完成失败重放、租约丢失和关闭等待线程。
- `npm run build` 与22项 Node 契约检查通过。Wiki chunk 706.69 kB 的大小提示仍待 M6 性能工作；不是构建失败。
- Python、数据库、TypeScript 和综合有限审查通过。审查发现并修复新目录 FD 泄漏、扫描器多来源锁顺序；Root 还修复重放父目录同步、读取边界及失败捕获的字节预算。
- 55432夹具与55433隔离验收应用库均为0012 head，`alembic check` 无待生成变更。迁移前保留应用库/RAG库 dump 和 Vault 压缩包。

## 真实移动后未完成日志的重启重放

合成来源 `273a4996-4fd9-41ed-b9e4-454a4fe5f5b5` 已删除且清理完成。0012 实际回填一项 queued 归档日志；正常关闭旧 API 后，独立进程领取、续租并执行真实目录归档，但没有完成数据库任务。保存过期租约后进程退出，不释放 owner。

新 API 在原件实际位于 Trash 时完成 Vault 哈希核验，接管同一日志 `f0e0c91c-7faa-4e9e-adec-08eafd1051d4`，attempts1→2、running→succeeded，原件哈希不变，健康检查 ready。

证据位于 `/tmp/knowgrain-m5-acceptance.TBHydm/trash-restart-pending.json` 与 `trash-restart-completed.json`。这是持久意图、真实 durable rename 和过期租约重启重放；没有执行物理断电、强制终止系统或宣称覆盖全部设备故障。

## 真实 Web/Core 删除、证据与恢复

合成来源 `2b10760a-78b5-48e8-8a58-036854c1a920`，使用嵌入式 LightRAG 1.5.7、PostgreSQL/pgvector、本机 Ollama 配置 `qwen3.6:35b`、`qwen3-embedding:0.6b`/1024维。Core 重索引可以复用保留的模型缓存，不宣称每次都触发新的 LLM 推理。

| 步骤 | 实际结果 |
| --- | --- |
| Web 删除 | 生命周期4→5，两个修订的 Core 清理成功后归档任务成功 |
| 文件归档 | 两个原件都进入同一 Trash 来源目录，canonical 来源目录不存在 |
| 历史证据 | 已审阅 Wiki 标记引用来源变化；证据弹窗仍显示准确修订、原件哈希和摘录；实际下载 HTTP bytes 的 SHA 完全一致 |
| Web 恢复 | 两个原件搬回原路径，文件恢复成功，生命周期5→6，显示等待索引/current未建立 |
| Core 重索引 | 自动更新至 ready/current=latest；Wiki 实体接口 evidence_current/binding_current均true，返回4个实体 |
| 幂等与旧请求 | 重复同一恢复请求200，日志/尝试次数不增加；旧version4删除409 |
| 内容保留 | 基线18个文件均经安全读取核对哈希，包含准确 Trash 对应原件；全部未变化 |

最新原件 SHA 为 `445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`。已审阅 Wiki `2e7b533a-b379-426a-8912-efcd4a1bc75d` 的完整哈希仍为 `e9bc26be36e0752123ab7534d115cd336042d9590d8a97d3a40af40202e6897d`。历史证据 ID 为 `9f896323-dc17-56ce-8103-79b585c86f7d`。

临时证据：同目录 `trash-baseline.json`、`trash-archived.json`、`trash-restoring.json`、`trash-restored-final.json`、`trash-all-retained-hashes.json`；真实 API 日志 `trash-api.log`。备份/日志/合成资料不提交 Git。

## 限制与后续

浏览器在760×664正常窗口实际操作；截图仅在工具中观察，没有导出可下载图片。浏览器的原生下载保存位置仍未证实，HTTP下载内容已核验。

本机 macOS 文件原语已经实测；Linux 分支没有在 Linux 上实际验收。没有操作 Obsidian 桌面客户端。归档期间，Obsidian 指向 canonical 原件的相对链接可能暂时失效；证据摘录页保持可读，恢复回原路径后链接恢复，不改写已审阅正文或使用符号链接。

完整启动对账、备份恢复演练、从原件全量重建、完整图谱/自动 Wiki 语义关联、第三方服务适配和 M6 发布仍为后续必需交付。
