# M5 持久化 Core 维护验收记录

状态：持久化 Core 清理与恢复重索引节点本地验收通过；M5/M6 仍未完成。

## 已验证

- 全仓后端：明确选定 `knowgrain_test:55432` 的 `python -m unittest discover -s tests -q`，283 项通过，18.489 秒；日志 `/tmp/knowgrain-m5-core-tests.log`。新增维护清单跨部分失败、恢复、重试和第二次删除的 PostgreSQL 回归通过。
- 前端 TypeScript、20 项 Node 契约检查及生产构建通过。Wiki chunk 706.69 kB，构建提示超过建议值；性能优化仍属 M6。
- 两个隔离应用库显式升级到 0011，`alembic check` 均无待生成操作。验收升级前备份 `/tmp/knowgrain-m5-acceptance.TBHydm/pre-core-maintenance-app.dump`。
- Python、数据库、TypeScript 与综合审查通过。审查发现并修复三处清单丢失，以及清理/插入交错时旧 owner 重新插入风险。插入在取得 Core 写锁后核验租约；等待写锁期间失效的回归通过。Web 重试 busy 协调和操作错误保留已修复。

## 真实共享关系

独立合成 workspace `m5_shared_proof_094f0259b277`，内嵌 LightRAG 1.5.7、PostgreSQL/pgvector、Ollama qwen3.6:35b 与 qwen3-embedding:0.6b（1024 维）。只处理本任务创建的两个文档，未操作用户资料。

两份文档均抽取 `Aster Laboratory` 与 `Beacon Observatory` 的操作/所有关系。

| 检查 | 实际结果 |
| --- | --- |
| 删除前关系成员 | 两份文档的 chunk，weight=2 |
| 删除第一份 | 4 个文档存储记录与该 chunk 严格确认不存在 |
| 共享关系 | 第二份成员仍在，weight=1 |
| 剩余来源查询 | 实际 `aquery_data` 返回第二份 chunk |
| 从原件重新索引第一份 | 两份成员恢复，weight=2 |
| 原件 | 两个完整 SHA-256 保持一致 |
| 关闭 | 12 个存储成功关闭，PostgreSQL pool 关闭 |

实际执行进程完成 exit 0，`proof.json` stage 为 passed。临时证据 `/tmp/knowgrain-m5-core-proof-ee24d031/proof.json`，日志 `/tmp/knowgrain-m5-core-proof.log`，不进入 Git。该检查直接调用真实 Core；不代替应用租约或 Web 验收。

## 应用与 Web（当前进度）

旧 API 正常关闭并完成 12 个存储 finalization，新单进程 API 就绪，维护列表接口返回真实状态。

合成来源 `2b10760a-78b5-48e8-8a58-036854c1a920` 在 Web 删除后生命周期 2→3，两个修订的清理任务均 succeeded / attempts=1。Web 显示清理完成与“已清理索引，恢复后将重建”。核对原件后恢复为 active/version4，latest queued、current null；随后实际执行器进入 indexing，最终 ready/current=latest。原件完整 SHA-256 仍为 `445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`。实际 Wiki 实体 API 返回 evidence_current=true、binding_current=true 和 4 个实体；实体名称会随重新抽取变化，不声称与旧模型输出完全相同。

已审阅页面 `2e7b533a-b379-426a-8912-efcd4a1bc75d` 删除清理后完整哈希仍为 `e9bc26be36e0752123ab7534d115cd336042d9590d8a97d3a40af40202e6897d`。

## 恢复与迁移补充验收

- API 平稳停止后，在隔离验收库创建另一份合成来源 `273a4996-4fd9-41ed-b9e4-454a4fe5f5b5`，通过 repository 领取维护任务，保存空清单并设置已过期租约。独立进程退出时不释放 owner；新 API 进程重领同一任务 `a5fdeb77-8784-4a55-a706-28e4f6b751be`，attempts 1→2、running→succeeded。未索引文档经过严格 absent 检查。证据 `/tmp/knowgrain-m5-restart-proof.json`；这验证持久任务重启恢复，不等同于物理断电或 Core 删除中途崩溃。
- Web 重启期间出现详情连接错误；手动点击刷新后恢复操作可用，已索引/current 修订保持一致。没有丢弃浏览器编辑内容。
- 补充 PostgreSQL 套件 5 项通过。新增断言验证等待 Source 行锁后租约到期拒绝完成，以及跨来源、多修订、第二周期和旧周期 live owner 拒绝提交/保存。该锁测试先阻塞于 Source，不能称分别覆盖 Job/Maintenance 行锁等待。
- 新建独立合成数据库 `knowgrain_m5_backfill_f0d801bdc2`，显式升级到 0010，写入 deleted 来源两修订/epoch5 和 active 来源一修订，再实际升级0011。断言只建立两份 queued/attempts0 维护任务、保持 epoch5、不为 active 修订建立任务。证据 `/tmp/knowgrain-m5-backfill-proof.json`。没有降级或清空共享测试库/验收库。
- 新测试经数据库有限复审通过；尚未分别覆盖每层锁等待和所有旧周期状态组合，这不是物理故障全覆盖声明。

完整 Trash 归档、启动对账、备份恢复演练、图谱页面、自动 Wiki 语义关联和 M6 发布仍为必需后续交付。浏览器截图仅在工具中观察，尚未导出独立图片。
