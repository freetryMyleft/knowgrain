# M5 Core 调用门禁与严格关闭验证

状态：运行时基础已实现，真实本地关闭验证通过；完整重建与应用协调器仍待实现。

## 实现与失败行为

- 七个 Core 读写入口共用同事件循环的计数门禁。关闭同步拒绝新调用，已进入且等待写锁的调用也计入排空；读取保持并行，不在排空时持有写锁。
- 并发关闭共用保留的清理任务。默认调用者等待上限30秒；超时或取消不取消清理任务，并锁存需要进程重启。即使之后清理成功，也不能通过普通重试解除锁存。
- 成功证明绑定 Core 对象和初始化 epoch；新的初始化使旧证明失效。失败不清空 Core 引用，不报告 ready。
- 严格适配固定版1.5.7：三种向量缓冲先落盘，四种 LLM 角色和 Embedding 回调去重关闭并核验队列状态；保存原 parser executor 并等待线程退出；逐个捕获十二个存储 finalizer，核验数据库引用、向量缓冲、ClientManager 和实际 asyncpg pool/holder。缺失协议或额外资源所有者拒绝成功证明。
- 不自动重置 shared storage 或创建另一代 Core。应用全局持久化锁存、代协调器和重建执行器属于后续工作；本节点的运行时锁存不能单独证明跨 Runtime 替换安全。

## 自动化与审查

Luna 使用 `gpt-6-luna` / `xhigh` 实现并通过62项重点检查、编译和 diff 检查。Root 按实际模块名运行：

```sh
.venv/bin/python -m unittest tests.test_core_lifecycle tests.test_generation_runtime tests.test_lightrag_core_maintenance tests.test_index_identity tests.test_provider_roles tests.test_lightrag_entity_mapping tests.test_core_inspection tests.test_application_runtime -q
```

实际102项通过，日志 `/tmp/knowgrain-core-close-focused.log`。首次命令使用了两个不存在的模块名，退出1；修正模块名后通过，不将首次描述为通过。测试包含实际固定版异步模型队列、真实线程退出等待、关闭竞态、取消/超时、释放失败、缺失字段和同循环限制；该套件不等于全仓 PostgreSQL 集成重跑。Ruff 未安装，本次未安装工具或更改依赖。

独立 Python 与通用代码审查通过。最终 Python 有限复审检查新增跨循环拒绝回归，并核对真实关闭证明。Architect 最终只读审查通过实现、契约顺序与实际证明，未重复运行测试；后续协调器必须同时要求匹配的成功证明与未锁存失败。

## 真实 PostgreSQL 与本地模型验证

在同机隔离恢复库 `lightrag_restore_20261003:55433` 和 Research Vault 副本上使用实际 Runtime/Core 启动。沿用本地 Ollama `qwen3.6:35b`、`qwen3-embedding:0.6b`（1024维）。未向原8787数据库或用户私有 Vault 写入。

1. 控制一次实际 `Knowgrain` 实体成员读取在进入门禁后等待，发起关闭；新读取被拒绝，存储和 pool 保留。
2. 放行已有读取，得到当前修订的真实 chunk 成员。
3. 在固定版实际 native parser executor 中运行受控合成线程。上游停止标志已设置且 executor 字段已清空时，关闭仍等待原线程，存储保持有效。放行后线程实际退出。
4. `last_close_proof.succeeded` 断言通过：六项标志全为 true、errors 为空；十二个存储已释放、pool 完全关闭、ClientManager 为零引用、parser 线程全部退出。重复关闭保留同一个证明对象。
5. 夹具仅在成功证明之后显式重置模块共享状态。前后十八份保留文件 SHA-256 全部相同。

脚本 `/tmp/knowgrain-core-close-acceptance.py` 退出0，日志 `/tmp/knowgrain-core-close-acceptance.log`。原始证明：

`/private/var/folders/s4/t2mnvcrn6bj30rbbpjbqd5g40000gn/T/knowgrain-close-20261003.d9rd3rx9/proof.json`

脚本最后 stdout 的 `close_succeeded` 为 false，原因是将 `asdict()` 保留的空 tuple 与空 list 比较；此前直接检查 `CoreCloseProof.succeeded` 已通过，保存的六项标志和空错误亦证明成功。该显示错误不是生产代码释放失败。

后续将临时脚本摘要直接读取 `final_proof.succeeded`，重新运行同一只读验收，退出0，stdout `close_succeeded: true`。新证明位于 `/private/var/folders/s4/t2mnvcrn6bj30rbbpjbqd5g40000gn/T/knowgrain-close-20261003.jd0088cr/proof.json`，日志 `/tmp/knowgrain-core-close-acceptance-final.log`；六项标志、空错误和十八份哈希再次一致。未修改生产代码以修复脚本显示。

2026-10-04 继续工作时，原8787执行句柄76532已不存在、端口未监听，确认进程缺失后按原配置恢复 API。现执行句柄56692，日志 `/tmp/knowgrain-m5-acceptance.TBHydm/core-close-node-api.log`；就绪字段全部 ready、reconciliation complete。新进程加载本节点代码；不是因观察超时重复启动仍运行的实例。

本次有真实数据库读取、真实 Core/队列/pool 和真实 executor 线程，但受控线程不是大型文件 native parser 验收；没有模拟物理断电。没有新增 Web 操作、迁移、依赖或前端改动。Docker/PG18、新机器安装、完整重建、完整图谱与 M6 尚未验收。
