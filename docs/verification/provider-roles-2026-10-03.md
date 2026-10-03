# 独立模型角色基础验证

本节点验证内部角色配置和异步协议适配，未接入应用模型设置、代账本或配置匹配围栏。默认8787实例继续使用既有 Ollama 路径；不能据此宣布第三方模型 Web 切换或 M5 全量重建完成。接口说明见 [模型角色适配层](../provider-role-adapters.md)。

## 自动化与审查

根任务执行：

```sh
.venv/bin/python -m unittest tests.test_provider_roles tests.test_index_identity tests.test_application_runtime tests.test_generation_runtime tests.test_lightrag_core_maintenance tests.test_lightrag_entity_mapping tests.test_core_inspection -q
```

实际83项通过，包括15项新角色测试；没有重跑无关前端构建或完整数据库套件。新测试覆盖四种角色组合的准确请求、独立凭据、格式和前缀、真实固定版 EmbeddingFunc 包装、向量顺序及数值、截断/拒绝/工具响应、错误脱敏、超时和取消关闭。

Luna 编写初版后停在 pending_init；根任务明确中断该任务并接手有限返工。修复默认端口规范化、不可哈希的非法 context/history、超大 sampling 数值，以及 refusal/deprecated function_call。新增回归检查后15项通过。独立安全/通用与 Python 复审各自实际执行15项并通过。新审查线程一度被宿主线程上限拒绝，随后复用了已有审查任务；不声称所有新线程成功启动。

Architect 最终只读复审通过内部基础、配置边界和实际验证记录，没有重跑数据库验证。此前8787进程26132和执行句柄82508已不存在，端口也未监听；明确确认后按原数据库/Vault/workspace配置重启。就绪检查全部ready，启动对账checked=1/healthy=1/repair_queued=0，日志 `/tmp/knowgrain-m5-acceptance.TBHydm/provider-node-api.log`。没有因观察超时重启仍运行的进程。

## 实际本机调用

运行 `/tmp/knowgrain-provider-acceptance.py`，使用本机 Ollama 的原生协议与 `/v1` 兼容协议，LLM 为 `qwen3.6:35b`，Embedding 为 `qwen3-embedding:0.6b`、1024维。兼容服务仍是本机 Ollama，不声称实际联通某一家云服务。

- 四组独立 LLM/Embedding provider 配置均通过 EmbeddingFunc 包装器实际调用文档和查询向量。每组 shape 为 `(1,1024)`，数值有限；同一输入使用不同前缀时，返回向量不同。
- 原生与兼容 LLM 均通过真实 JSON Schema 请求，得到准确的 `{"status":"ready"}`。首次使用普通 JSON object 的脚本收到合法的 `{}`，未满足脚本要求，因此退出1；当时尚未创建 Core 或写数据库。后续使用明确字段约束的 Schema 继续验证，不将首次结果描述成字段验证通过。
- 第二次脚本同一事件循环构造单个 fresh Core：LLM 使用兼容协议，Embedding 使用原生协议。目标 `kg_provider_4981241ab06b928a01d3b8bb`，向量 token `kg_4981241ab06b928a01d3b8bb`；真实模型名保持独立。
- 在隔离恢复 RAG 库 `lightrag_restore_20261003` 上完成 `ainsert` 和 `aquery_data`，返回7实体、6关系、1文本块。文本块归属准确修订 `56ffa83e-99f2-48dd-87cf-e888070aaf87`。十二存储工作区一致，有限文档检查 healthy；不以此证明严格完整图谱审计或语义正确率。
- 旧 namespace 原文SHA保持一致；所有十八个保留的原件、Wiki、证据及隐藏日志文件哈希未变。应用数据库的 active 选择和来源任务未修改。
- 脚本手动关闭 Embedding 队列，再用既有 Runtime 关闭各 LLM 角色与存储。验证十二 storage.db 为空、PG ClientManager 引用计数0/db为空、pool关闭、三 vector pending upsert/delete为空，随后手动清理模块 shared state。这不是应用已实现的自动换代协调器。

第二次脚本退出0。证据：

- `/tmp/knowgrain-provider-acceptance.log`：第一次脚本字段断言失败。
- `/tmp/knowgrain-provider-acceptance-schema.log`：实际协议调用、Core 索引检索与正常关闭。
- `/private/var/folders/s4/t2mnvcrn6bj30rbbpjbqd5g40000gn/T/knowgrain-provider-20261003.sda4yvl4/proof.json`：配置、检索计数、保留文件哈希与关闭证明。

临时脚本、日志与合成数据库记录不提交Git。尚需完成实际完整索引 profile、持久化代与模型匹配、重建执行器、Web 设置和真正配置的第三方服务验收；M6 评估、性能、安装发布仍待完成。
