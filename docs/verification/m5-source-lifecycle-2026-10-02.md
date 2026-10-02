# M5 来源生命周期节点本地验收

日期：2026-10-02。范围：逻辑软删除、原件核验恢复、索引任务租约、Web 状态协调。整体 M5/M6 尚未完成。

## 自动化与审查

- `KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 .venv/bin/python -m unittest discover -s tests -v`：240 项通过，16.881 秒，无跳过。使用隔离 PostgreSQL 测试库；输出 `/tmp/knowgrain-m5-lifecycle-tests.log`。
- 来源 PostgreSQL 套件包含 CAS、重复请求、删除/恢复 ABA、修订变化、删除运行任务后拒绝迟到完成、已删除来源跳过 claim、过期不能续租、等待来源锁期间到期不能完成、过滤后分页。
- 服务/API 检查严格请求字段、原件缺失/变化/不安全读取、取消时线程收尾；索引检查确认读取线程结束后才关闭执行任务。
- 实际 65 MiB 原件读取和 SHA-256 校验通过，101 MiB 拒绝。原件读取与上传配置共享 100 MiB 支持上限，默认上传仍为 20 MiB。
- `npm run build`：TypeScript 与 Vite 通过。`node --test checks/*.test.mjs`：18 项通过。
- Python、数据库与最终综合审查通过。TypeScript 审查指出上传后的旧筛选闭包与操作中切换资料问题，Root 修复；其最后回复受宿主用量限制中断。综合审查另指出轮询抢先覆盖详情的竞争，修复后增量审查通过。
- 0010 非破坏性迁移显式应用到测试及验收应用数据库；API 不自动迁移。验收库迁移前以 pg_dump 保存快照于本机临时目录。
- 两个隔离应用库分别执行 `alembic check`，均无待生成的升级操作，迁移和 ORM 一致。

## 实际 Core/API/Web 流程

环境：PostgreSQL 16.14 / pgvector 0.8.1、内嵌 LightRAG 1.5.7、Ollama qwen3.6:35b + qwen3-embedding:0.6b（1024 维）、单后端进程 localhost:8787。验收只操作本任务创建的合成来源。

来源 `2b10760a-78b5-48e8-8a58-036854c1a920`，latest/current 修订均为 `56ffa83e-99f2-48dd-87cf-e888070aaf87`、ready。Web 首先打开删除确认并取消，然后实际确认删除。

| 检查 | 删除前 | 删除后 | 恢复后 |
| --- | --- | --- | --- |
| 来源状态 / lifecycle_version | active / 0 | deleted / 1 | active / 2 |
| Wiki 实体关联数 | 3 | 0 | 3 |
| 已保存问答证据当前性 | true | false | true |
| active 列表 | 包含来源 | 不包含来源 | 包含来源 |
| 原件 SHA-256 | 精确匹配 | 精确匹配 | 精确匹配 |
| 已审阅 Wiki | 基线 | 与基线相同 | 与基线相同 |

原件完整哈希：`445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`。保留证据 `9f896323-dc17-56ce-8103-79b585c86f7d` 的原件 HTTP 下载在删除期间仍返回相同字节哈希。

删除期间实际提交新问题“这份验收资料说明原件保存在什么 Vault 中？”。任务 `b9d2ccb4-ade0-4fec-8da2-e77a5fc18447` succeeded，结果 insufficient，无声明、无证据；结果标记当前模型 qwen3.6:35b。这验证实际检索不会把已删除来源交给新答案，不代表大规模质量评估。

Web 恢复前核验原件，自动切回有效资料列表；上传按钮恢复可用。重复删除/恢复请求均幂等；恢复后重新发送版本 0 的旧删除请求返回 409，来源仍 active/version 2。最后页面留在已恢复来源详情。

本机临时证据目录 `/tmp/knowgrain-m5-acceptance.TBHydm/` 保存 baseline/deleted/restored JSON、已删除期间的问答结果、迁移前应用库备份与 API 日志；它们不进入 Git。浏览器截图仅在工具中观察，未导出为独立文件。

## 实现边界与后续

此节点保留原件、Wiki、current 指针及已完成 Core 索引，允许核验后恢复。软删除取消排队/运行的索引任务，当前性过滤立即生效；Core 的废弃记录物理清理与 Trash 移动尚未实现。

下一节点需要带持久化日志的 Core 清理和 Trash 归档/恢复，保护共享关系的有效来源成员，并验证强制重新索引、启动对账与备份恢复。完整关系可视化、自动语义 Wiki 关联、第三方模型配置和 M6 发布评估仍是必需交付；不能把本节点解释为整个 M5 已完成。
