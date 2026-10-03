# M5 Core 工作区身份基础验收

状态：身份基础通过自动化与真实本地 Core 验证。完整重建的持久化代、执行器、严格全存储审计、原子激活与 Web 操作仍未交付；设计见 [全量重建契约](../m5-workspace-rebuild-contract.md)。

## 实现

- 不可变 `CoreIndexIdentity` 保存准确 workspace、working 目录及可选向量存储 token。默认 legacy 保留无后缀向量表；显式 target 的 `kg_` 加24位小写 hex token 只影响存储表，真实模型仍由 Settings 指定。
- 固定版实际读取 `POSTGRES_WORKSPACE`，日志称为 `PG_WORKSPACE`；运行时显式绑定两个值，核对十二个实际存储的 workspace。缺失或不同则拒绝 ready，执行清理并要求重启。
- 默认 legacy 在覆盖环境之前，调用固定版真实配置解析器核对有效旧 workspace。环境/config.ini 的覆盖值不同于 `LIGHTRAG_WORKSPACE` 时，在 PostgreSQL 探测、模型验证和存储初始化之前拒绝，不静默迁移索引。
- 三种实际向量表名在 Core 构造前检查 PostgreSQL 的63字符限制。配置模型/维度的完整持久化匹配与换代策略尚待后续实现。
- 没有新增依赖、迁移或公开 API。

## 自动化与审查

根代理执行：

```sh
.venv/bin/python -m unittest tests.test_index_identity tests.test_application_runtime tests.test_generation_runtime tests.test_lightrag_core_maintenance tests.test_lightrag_entity_mapping tests.test_core_inspection -q
```

实际68项通过。包含17项身份测试，以及现有生命周期、配置回调、实体映射、清理和当前修订检查；不声称本次重新执行全部 PostgreSQL集成或前端构建。

新测试用真实固定版 LightRAG 构造，替换网络初始化；测试分别证明 legacy 表名兼容、目标向量 token、十二存储身份、配置优先级、默认冲突拒绝、显式覆盖、表名限制及失败清理。该测试本身不证明真实连接池释放。

通用审查发现的旧 workspace 静默切换问题已修复，最终通用/Python复审通过。Python复审实际执行17项身份测试并比对完整环境，变化键为空。architect 审查并通过补充后的大型重建契约；这是设计批准，不是重建功能验收。

## 真实 PostgreSQL / Ollama 验证

使用上一节点的隔离恢复 RAG 库 `lightrag_restore_20261003`，PostgreSQL16.14/pgvector0.8.1，Ollama `qwen3.6:35b` 与 `qwen3-embedding:0.6b`（1024维）。没有改动应用数据库里的 active 选择或索引任务，也没有向原实例 RAG 库写入测试目标。

1. 在单个 asyncio 事件循环启动 legacy `knowgrain_setup_acceptance`，读取已恢复的准确修订原文，SHA与解析基线相等；十二存储仍在 legacy namespace，三向量表仍无后缀。
2. 验证脚本手动补充 Embedding 队列关闭、检查 client/pool 引用并清理共享状态，再启动显式目标 `kg_identity_e6c5d45331852cf0eb7d2d79`。脚本故意设置冲突 ambient workspace，实际十二存储全部绑定到明确目标。
3. 原件安全读取/重新解析后，以原 revision UUID 调用真实 `index_text`；日志证明创建三张新的 token 向量表，未迁移该目标的 legacy 数据，实际抽取4实体、3关系，并落盘4/3/1个实体/关系/块向量。
4. 首次脚本错误地读取检查结果 `healthy` 键；实际接口为 `state`。Core写入已完成，脚本正常清理后退出1。修正脚本后继续同一目标，没有再生成 namespace 或重新索引。
5. 继续检查返回 `state=healthy`；真实 `retrieve` 返回4实体、3关系、1文本块，块的来源修订为 `56ffa83e-99f2-48dd-87cf-e888070aaf87`。
6. 同一连接直接读取旧 namespace，原文SHA未变；三张目标向量表名均≤63字符。关闭后十二存储 db引用为空、ClientManager引用计数0且db为空、pool关闭，真实 vector pending upsert/delete缓冲为空。
7. 全部十八个原件/证据/Wiki及隐藏日志文件的安全读取SHA与恢复基线相等。

脚本手动补充的 Embedding shutdown/shared reset **不是应用已实现的自动换代协调器**。本验收证明明确身份能使用独立存储并继续真实检索，不证明关闭故障注入、所有记录集合的严格健康或完整重建恢复。

## 运行状态与证据

中断后的检查确认原8787 API进程不在，两个隔离PostgreSQL与Ollama仍运行。使用既有应用库 `knowgrain_test_setup_ui`、RAG库 `lightrag_acceptance` 与原绑定Vault重新启动8787 API，准确绑定原workspace；就绪接口全部ready。启动对账 complete、checked=1、healthy=1、repair_queued=0。

本地合成证据：

- `/tmp/knowgrain-identity-acceptance.log`：第一次实际索引、正常清理及验证脚本字段错误。
- `/tmp/knowgrain-identity-acceptance-resume.log`：继续同目标后的实际检索和正常关闭。
- `/private/var/folders/s4/t2mnvcrn6bj30rbbpjbqd5g40000gn/T/knowgrain-core-identity-20261003.d_cst5c4/proof.json`：两代实际存储身份、表名、检索计数、旧文档保留和十八文件哈希。
- `/tmp/knowgrain-m5-acceptance.TBHydm/identity-api.log`：原实例新进程启动与就绪记录。

临时脚本/日志/数据没有提交到Git。未验证Docker/PG18、新机器、Obsidian桌面，也没有对完整M5或M6作完成声明。
