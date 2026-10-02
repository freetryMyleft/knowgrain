# M5 Trash 与文件恢复契约

状态：Trash 归档/恢复节点已完成本地验收；见 verification/m5-trash-recovery-2026-10-02.md。完整 M5 的启动对账、备份恢复演练和全 Vault 重建仍未完成。已验收的 Core 维护节点见 m5-core-maintenance-contract.md。

## 内容身份与目录

- source_revision.vault_path 继续表示不可变的原始逻辑路径 `Sources/Files/{source UUID}/{revision UUID}{suffix}`，不得改写历史问答/Wiki 中保留的身份。
- 删除来源的原件实际归档到 `Trash/Files/{source UUID}/`；恢复时回到同一 `Sources/Files/{source UUID}/`。原件目录移动不触碰 Wiki、证据页、其他来源或 `.obsidian`。
- 同一目录包含该来源所有已登记修订；文件列表与哈希核验后才移动。未登记文件、符号链接、非普通文件、缺失、哈希不符或两边同时存在均是冲突，不自动删除或合并。
- 普通文件读取仍要求 Vault 根目录内、目录 FD、不跟随符号链接、支持大小上限、前后身份一致与完整 SHA-256。历史证据解析只允许确切来源/修订对应的已核验物理位置；不能让浏览器指定任意路径。
- 新机器恢复内容映射、应用数据库和 Core 全量修复、备份演练仍为本阶段必需后续，不以一次文件移动替代。

## 文件层（先冻结独立接口）

新增 `source_archive_files.py`，不操作数据库或 LightRAG：

```python
@dataclass(frozen=True)
class ArchiveEntry:
    revision_id: UUID
    vault_path: str
    sha256: str

class SourceArchiveFiles:
    def __init__(self, vault: VaultStore): ...
    def archive(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> None: ...
    def restore(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> None: ...
    def location(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> Literal['vault', 'trash']: ...

class SourceArchiveFileError(RuntimeError):
    code: Literal['missing', 'conflict', 'unavailable']
```

每次同一来源 1–10000 个不同修订；严格验证规范 UUID、已支持后缀、精确 canonical path、64 位小写 SHA-256。不允许字符串当 entries、任意目标目录或其他来源成员。

archive/restore 同向重试时：若起点不存在而终点存在且精确成员/哈希全部一致，则成功；两端同时存在或都不存在均失败，不覆盖目标。通过目录 FD、安全目录检查与原子目录 rename 移动，严格复核移动前后目录身份和文件成员。目标父目录按需创建并保持 durable；原件文件不用复制为不完整目标、不 unlink、不永久删除。

移动后 fsync 起点/终点父目录，失败报告 unavailable 而不声称成功；再次调用按实际位置核验。恶意/外部并发改变目录的窄窗口必须 fail closed，保留实际文件供日志对账，不能用未经核验的内容宣布恢复。不要自行生成伪成功或回滚覆盖外部新文件。

严格目标不覆盖：使用标准库 ctypes 调用平台原子 no-replace rename（macOS renameatx_np / Linux renameat2），核对本机头文件/官方文档的 flags。平台不支持或跨文件系统时 unavailable，不回退到可能覆盖空目录的 os.rename。未登记额外文件保留在原目录并报告冲突，用户修复后重试。

应用调用方必须先持久化移动意图、取得同一文件写锁后重新验证租约/来源周期，且取消时等待文件线程结束。独立文件层测试可以直接调用接口；它不是提供给浏览器的 API。

## 统一原件读取（冻结）

EvidenceAccess 新增 `capture_original(relative, maximum_bytes, *, allow_archived=True) -> tuple[bytes,str]`：返回完整捕获的 bytes 与 SHA-256。复用既有目录 FD/no-follow/stat 检查；max 必须为 1–MAX_UPLOAD_BYTES 的非 bool 整数。仅严格 canonical `Sources/Files/{sourceUUID}/{revisionUUID}{supported suffix}` 的 missing 可推导确切 `Trash/Files/{sourceUUID}/{revisionUUID}{suffix}`；不搜索文件、不在 conflict/unavailable 时回退。原始逻辑路径不改变。

`original()`、`original_revision()` 使用该捕获并验证保存的 SHA；original_revision 新增 keyword `allow_archived=True`，索引执行器传 false，只索引搬回后的 canonical 文件。普通 evidence Markdown 读取/发布不能回退到 Trash。

Provenance `_load_verified_original` 使用 capture_original 保留自己的总字节预算、来源/解析哈希与位置验证；内容哈希不符时仍扣除已读取字节。VaultSetup 原件核验使用 original_revision 接受 exact archived 原件；缺失/变化/unsafe 不转为就绪，不放宽数据库当前证据资格。

Obsidian 的历史原件相对链接在归档期间可能暂时失效；证据摘录页/block 保持有效，恢复后原件相对链接恢复。不通过 symlink 或改写 reviewed 正文掩盖此限制。

## 数据库日志与 Repository（冻结）

SourceFileOperation：UUID id、source_id FK SourceDocument ON DELETE CASCADE、lifecycle_version BIGINT≥0、kind archive/restore、state queued/running/succeeded/failed/cancelled、manifest JSONB（固定全部历史修订的 revision_id/vault_path/sha256）、attempts、lease_owner、lease_until、error、created_at/updated_at。restore 保存 expected_latest_revision_id、verified_current_revision_id，用于重试/重启完成原有 CAS；相同来源/周期/kind 唯一；每个来源最多一项 queued/running 的 partial unique index。新增显式 0012_source_file_operations 迁移和 schema head。

锁顺序 Source → index jobs → maintenance jobs → file operations → revisions，所有同 source 操作保持顺序。文件任务租约检查使用取得所有锁之后的 clock_timestamp，90 秒期限、20 秒续租。归档/恢复文件不递增 source lifecycle，现有 tombstone/激活继续各递增一次。

SourceFileRepository(database, sources: SourceRepository) 接口：

```python
enqueue_archive_if_cleaned(source_id: UUID) -> dict | None
prepare_restore(source_id: UUID, *, expected_lifecycle_version: int,
    expected_latest_revision_id: UUID | None) -> dict
claim_file_operation(owner: UUID, *, operation_id: UUID | None = None) -> dict | None
renew_file_lease(operation_id: UUID, owner: UUID) -> bool
complete_file_operation(operation_id: UUID, owner: UUID) -> dict | None
fail_file_operation(operation_id: UUID, owner: UUID, safe_error: str) -> bool
release_file_owner(owner: UUID) -> None
retry_file_operation(operation_id: UUID) -> dict
list_file_operations(source_id: UUID, *, limit=100) -> list[dict]
enqueue_cleaned_sources(*, limit=100) -> int
```

claim 内部快照包含 operation_id/source_id/lifecycle_version/kind/manifest/expected_latest_revision_id/verified_current_revision_id。PUBLIC快照仅 operation_id/source_id/lifecycle_version/kind/state/attempts/error/created_at/updated_at/lease_until，不返回 lease_owner 或文件清单。所有来源/操作缺失使用现有 SourceNotFoundError，冲突使用 SourceConflictError，公开异常不得含用户文件内容。

enqueue_archive_if_cleaned 必须检查当前删除周期全部已登记修订都有 succeeded CoreMaintenanceJob；不能以没有 running/queued 代替。manifest 固定当前所有历史修订、严格排序/去重/边界。SourceRepository.complete_maintenance 最后一个成功时在同一事务调用独立 enqueue_archive_for_source(session, source) helper；启动/周期 bounded scan 也补齐漏建日志。0012 为此前已清理完的 deleted 来源回填同等日志。

prepare_restore 在来源锁内做原有 state/version/latest CAS；阻止任意 running Core maintenance 或文件任务（含过期）。取消未运行 archive 及旧 queued restore，创建或幂等复用同周期 restore 操作，manifest 包含所有修订。恢复文件意图提交后即可由新进程重领，不依赖 HTTP 连接。已经按该请求激活则返回内部 `already_restored=True, source=<snapshot>`，不再次搬动。

complete_file_operation 要求严格 live owner、source仍deleted、相同lifecycle及manifest与DB修订元数据一致。archive 只标日志成功；restore 在同一事务标日志成功并执行现有 SourceRepository.restore 激活/force reindex（提取同session内部方法），并返回source snapshot。已发生移动而 lease/commit 失败时不宣称成功，保留日志用实际位置重试。SourceRepository.restore直接入口若存在执行过/未取消的归档文件日志，必须拒绝绕过文件恢复；只有精确当前周期 restore op 的完成路径可调用内部激活。已有无文件日志的 repository 测试可继续旧恢复行为。

新删除操作拒绝该source任何queued/running restore；Core 清理完成不能为已经准备恢复的来源另排 archive。文件旧周期不能保存/续租/完成/失败或重试新周期的操作；release不复活旧周期，确切未完成任务回到queued（由幂等实际位置核验恢复）。每个执行不接受客户端路径或清单。文件移动前执行者必须在共用文件写锁里续租并重新验证当前操作。

## 应用与 API（冻结）

SourceFileService 与 SourceFileRunner 共用同一 asyncio file lock，归档和同步恢复都从已持久化 manifest 构造 ArchiveEntry。cancel/lease丢失后线程先drain，再释放文件锁/owner。文件 runner 仅依赖应用 DB/Vault，不以Core/Ollama就绪作为门槛。Vault切换/重试/关闭先停止文件执行者，然后替换引用；首版仍单进程。

同步 POST restore 保持现有200 source snapshot：prepare_restore提交意图→核验/回移全部修订→同事务complete激活。冲突409、暂不可用503；失败保留可重试journal，重启后完成已记录恢复意图。重索引仍异步，完成前 current=null。

GET `/api/v1/sources/{source_id}/file-operations?limit=100` 返回PUBLIC数组；POST `/api/v1/file-operations/{operation_id}/retry` 返回202 queued PUBLIC快照。Web显示归档/恢复进度及失败重试，不把归档当作永久删除。恢复/删除/重试/上传/导航保持相同busy协调。

新索引传 allow_archived=False；active索引不能偷偷从Trash读取。历史原件下载可按稳定canonical+hash回到精确Trash原件。启动对账、完整备份恢复和全Vault重建仍需要另外验收。

## 验收

文件层：多修订 archive→历史安全读取→restore→哈希不变；重复操作；目标碰撞/多余成员/哈希变化/缺失/符号链接/FIFO拒绝；取消时 drain；目录 fsync/移动后失败的恢复。

集成：真实 DB/文件/Core/Web 删除→归档→原件证据跳转→恢复重索引；重启接续；旧周期拒绝移动；已审阅 Wiki 零覆盖；Vault 原件可从备份重建。操作 Obsidian 客户端和跨平台目录移动的具体限制必须如实记录。
