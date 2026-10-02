# M5 持久化 Core 清理与强制重索引

状态：实施中。继来源软删除节点之后实现，不替代 Trash、启动对账或完整备份恢复。

## 不变量

每个删除周期为每份来源修订在应用事务内建立维护任务。后台调用内嵌 Core 的公开 `adelete_by_doc_id`，不直接删除共享实体/关系，不启动另一个服务。Core 按剩余来源重建共享关系。

清理和索引共享同一 asyncio 写操作锁；查询仍按当前来源/修订/哈希过滤。删除前将有界的文本块成员清单持久化，取消/关闭时等 Core 操作收尾后再释放任务和存储。

原件、历史证据、已审阅 Wiki 在本节点保留。归档到 Trash 的事务日志、实际路径解析及失败恢复另一个节点实现，不允许假装已完成。

## 数据库与 Repository

新增 `CoreMaintenanceJob`：id UUID，source_id/revision_id（复合外键确保同一来源，删除测试修订时级联清除任务），lifecycle_version BIGINT，state queued/running/succeeded/failed/cancelled，attempts、lease_owner、lease_until、error、created_at/updated_at，cleanup_chunk_ids JSONB nullable。唯一键 (revision_id,lifecycle_version)。索引覆盖领取状态/租约、来源/周期。

Job 新增 force_rebuild Boolean 默认 false、cleanup_chunk_ids JSONB nullable。显式 0011_m5_core_maintenance 迁移，schema head 同步。

- soft_delete 在同一事务创建该周期的每份修订 cleanup 任务；重复请求不重复排队。
- restore 在来源锁下检查所有该来源的 running 清理（即使租约已过期也不直接激活来源）；有运行任务返回 409。取消 queued 和尚未执行的失败任务；如果任何清理已经开始（attempts > 0），清空 current 指针并把 latest 修订的 index job 重新排队、force_rebuild=true。原件核验与版本/current CAS 保持不变。
- 现有 retry_source 设置 force_rebuild=true；恢复、重试及新的删除周期必须合并同一修订的索引和维护任务已有清单，防止部分清理失败后文档状态缺失而漏查残留块。清单并集验证并限制到 10000 项；在重新处理成功之前不能给新答案使用。
- 任一旧维护任务只在 source deleted 且 lifecycle_version 相符时可执行/提交；restore 后旧任务 cancelled，不得再删除当前 Core 数据。
- 锁顺序 Source → index Job → MaintenanceJob → Revision；相关方法统一顺序，clock_timestamp 在取得所有锁后读取。维护 claim 使用 skip_locked；renew、完成、失败、清单保存均严格租约校验；释放任务不复活旧周期。

Repository 接口（UUID 输入、dict 快照，清单最多 10000 个不同有界文本块 ID）：

```
claim_maintenance(owner) -> dict | None
renew_maintenance_lease(job_id, owner) -> bool
record_maintenance_chunks(job_id, owner, chunk_ids) -> bool
complete_maintenance(job_id, owner) -> bool
fail_maintenance(job_id, owner, safe_error) -> bool
release_maintenance_owner(owner) -> None
retry_maintenance(job_id) -> dict
list_maintenance(source_id, limit=100) -> list[dict]
record_index_cleanup_chunks(job_id, owner, chunk_ids) -> bool
```

claim 返回 job_id/source_id/revision_id/lifecycle_version/cleanup_chunk_ids；任务公开快照不返回 lease_owner 或清单。index claim 返回 force_rebuild 和 cleanup_chunk_ids，成功完成后 force_rebuild=false；下一次用户 reindex 保留同一修订的历史清单，直至严格证实记录已不存在。

## Core 适配器

```
delete_revision(*, source_id: str,
 expected_chunk_ids: Sequence[str] | None = None,
 persist_manifest: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
 delete_llm_cache: bool = False) -> None
```

持有与 index_text 相同的写锁。仅接受规范 UUID 修订；读 doc_status 的 chunks_list，校验块成员 full_doc_id，合并此前持久化清单，调用 persist_manifest 并成功返回后才允许删除。callback 必须执行最新任务租约/来源状态检查，失效即抛错。成员超过边界或混入其他修订时失败，不能删除。

规范化 DeletionResult，只允许相同 doc_id 的 success 或 not_found；not_allowed/fail/结构错误为安全的可重试错误，不透传上游私有消息。删除后严格检查 doc_status、full_docs、full_entities、full_relations 以及清单中的 text_chunks 均已不存在。not_found 不等于成功；若还有残留则失败并保留日志，不能宣称索引已经清理或重建。缺失状态且有孤立数据时需显式修复/完整重建，禁止悄悄丢失证据。

## 执行与接口

新 `CoreMaintenanceRunner` 与 IndexJobRunner 共用后端事件循环。按维护租约运行，20 秒续租；调用 Core 前通过 persist_manifest 记录清单并重新验证当前租约。Core 操作即使收到取消也必须等其收尾，最后失败/释放保留安全恢复状态。记录错误使用固定消息，不记录用户资料。

IndexJobRunner force_rebuild=true 时，在安全读取和解析原件后先执行 delete_revision（delete_llm_cache=true）；persist_manifest 调用 record_index_cleanup_chunks，失败阻止删除。删除确认完成后 ainsert，再根据严格 Core 文档状态完成 index job。普通初次导入保留现有逻辑。

应用在初始化、Vault 选择、关闭时统一启停维护执行器；关闭所有写执行器后才关闭 Core。运行数据库/Vault/Core/模型不可用时任务保持排队。

新 HTTP：GET /api/v1/sources/{source_id}/maintenance（limit 1–100，缺失来源404）；POST /api/v1/maintenance/{job_id}/retry（仅当前 deleted 周期可重试，running/已成功/已取消409）。浏览器只访问 Knowgrain API，界面显示清理状态及重试入口。恢复后索引排队必须清楚显示，不能声称关系已即时恢复。

## 验收

实际 PostgreSQL 验证创建/重复删除、租约过期/锁等待、取消旧周期、恢复时排队、清单持久化、失败重试、领取和 source 范围。实际 Core 对两个共享实体/关系的合成文档删除其中一个，检查其文档/文本块消失而另一份可检索，共享成员不被误删；从原件强制重建后关系回归。Web 显示清理状态及恢复索引状态；历史原件和已审阅 Wiki 哈希不变。重启/过期租约重领必须有实际证据，不能用一次成功调用代替恢复验收。

文件所有权：Luna Core 适配器拥有 lightrag_runtime.py 与新增适配器测试；Luna persistence 拥有 models.py/source_repository.py/database.py/0011 迁移及 PostgreSQL 测试；Luna executor 拥有 core_maintenance_runner.py/job_runner.py 与执行器测试。Sol 拥有应用/API/UI 集成、说明及最终实际验收。
