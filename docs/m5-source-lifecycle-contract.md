# M5 来源生命周期与索引租约契约

状态：实施中，2026-10-02。本文定义 M5 的首个交付节点，不代表 M5 全部完成。

## 本节点接口

- `GET /api/v1/sources?state=all|active|deleted`：默认 `all` 保留原 API 行为；Web 明确选择 active 或 deleted。分页在过滤之后应用。
- `DELETE /api/v1/sources/{source_id}`：逻辑软删除，返回完整来源快照，HTTP 200。
- `POST /api/v1/sources/{source_id}/restore`：核验 Vault 原件后恢复，返回完整来源快照，HTTP 200。

两个写接口必须提供 JSON：

```json
{"expected_lifecycle_version": 0, "expected_latest_revision_id": "修订 UUID 或 null"}
```

版本是严格非负整数，不接受布尔值或字符串数字。字段必填，禁止额外字段。
来源快照新增 `lifecycle_version`，数据库现有来源从 0 开始；每次状态转换递增一次。
写入持有应用 runtime lock，并重新检查数据库/Vault 就绪，避免与 Vault 绑定切换交错。

## 状态与并发

- 缺失来源返回 404。版本或 latest 修订不匹配返回 409；客户端刷新后再操作。
- 仅当目标状态已经达到、版本恰好等于 expected + 1、latest 修订仍匹配时，允许同一次请求重发成功；不再次递增。
- delete → restore 后旧 delete 不再满足版本检查，必须冲突；禁止 ABA 回退。
- 删除事务锁来源，再按稳定顺序锁该来源的任务和修订。排队/运行的索引任务转 failed、清除租约；未完成修订转 failed。已完成修订、current 指针、原件、Wiki 正文和历史证据保留。
- 新检索/生成/实体映射沿现有 active + latest/current + ready + 哈希规则过滤 deleted 来源。历史引用仍能读取保留的精确原件，但不能冒充当前证据。
- 恢复先读取快照，安全核验 latest/current 对应原件（去重），再事务比较版本、latest 和读取时的 current 指针。文件缺失/变更返回 409，读取服务故障返回 503，不恢复状态。
- 恢复不自动将失败任务标记 ready；可通过现有重试入口排队。单次 ainsert 跳过已有 Core 文档不等同于强制重建。

## 内部接口与所有权

Repository（Luna persistence）：
`soft_delete_source(source_id, *, expected_lifecycle_version, expected_latest_revision_id) -> dict`
`restore_source(source_id, *, expected_lifecycle_version, expected_latest_revision_id, verified_current_revision_id) -> dict`
latest/current 参数均使用 UUID 或 None。列表新增可选 `state='all'`。

Service（Luna service）：
`soft_delete_source(source_id, *, expected_lifecycle_version, expected_latest_revision_id) -> dict`
`restore_source(source_id, *, expected_lifecycle_version, expected_latest_revision_id) -> dict`
利用 `EvidenceAccess.original_revision(relative, expected_sha256) -> bytes` 安全有界读取，不伪造 Evidence。
新 `source_lifecycle_api.py` 导出 `install_source_lifecycle_routes(app)`，由 Sol 在 create_app 注册。

Persistence 所有权：SourceDocument 字段、source_repository.py、database.py schema head、0010 迁移、来源 PostgreSQL 测试。
Service 所有权：source_service.py、evidence_access.py、新生命周期路由、对应单元/API 测试。
UI 所有权：新 SourceLifecyclePanel、source-lifecycle-contract、CSS、Node checks；Sol 负责 App.tsx 集成和文档。

## 租约与验收

所有索引状态写入遵守 Source → Job → Revision 锁顺序。claim 通过非阻塞来源/任务锁避免反向锁死；跳过 deleted 来源。renew/complete/fail 在锁取得后读取 PostgreSQL clock_timestamp，要求来源 active、任务 running、owner 匹配且租约尚有效。shutdown release 重新检查状态，不复活删除任务。

验收包含实际 PostgreSQL 状态变化、重复请求/ABA/新修订冲突、删除运行任务后迟到完成被拒、过期租约不能续租、等待锁期间过期不能完成、文件被改动时恢复失败，以及 Web 的删除/已删除列表/恢复流程。

## 完整 M5 后续节点

仍需实现：持久化清理任务和幂等 Core 删除；带日志与冲突保护的 Trash 归档/恢复（原件路径与历史引用同步）；强制重新索引和旧修订清理；启动对账/崩溃恢复；Vault + PostgreSQL 备份、原件重建及实际恢复演练。不能以本节点的逻辑删除或普通 ainsert 重试替代这些要求。发布评估、第三方模型配置和 M6 门槛继续保留。
