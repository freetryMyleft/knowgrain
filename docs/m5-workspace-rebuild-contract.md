# M5 全量索引工作区重建契约

状态：设计与分工契约，尚未交付完整重建 API、执行器或 Web 操作。运行时身份基础另行验收；不能据此宣布 M5 完成。

内部 D1 账本与事务仓储已实现，迁移为 `0013_core_generations`。普通任务围栏、真实协调者排空、严格审计和激活尚未接入；细节及边界见 [账本契约](core-generation-ledger.md)。

## 1. 用户流程与验收范围

用户在 Web 发起“从原件重建索引”，查看范围和模型配置后确认。系统暂停来源修改和模型任务，从 Vault 安全读取每个活跃来源的最新修订，在全新的 LightRAG 工作区建立向量、实体和关系。全部来源验证通过后，一次性激活新工作区，再开放问答和生成。

- 原件、已审阅 Wiki、历史证据页、来源/修订/审阅身份保持不变。
- 重建目标不包含已删除来源或活跃来源的历史修订。
- 不删除旧工作区或旧向量数据；旧数据保留用于检查和后续显式维护。
- 进程中断后继续同一个持久化目标，保留已经完成的来源进度。
- 缺失或被外部修改的原件、模型不可用、Core 数据写入/读取失败时保留失败记录，禁止激活不完整索引。
- 该功能修复应用数据库仍完整、PostgreSQL 可正常读写且共享 schema 正常时的派生工作区数据损坏。目标初始化前严格核对共享 schema/FK 已就绪及新 workspace 在旧向量表中也没有数据；缺失或破坏 schema 进入独立恢复。固定版 Core 初始化可执行全表 DDL、清扫无 FK 的孤儿边、迁移旧向量或移除空旧表，不能以 fresh workspace 保证绕过这些副作用。应用库丢失后的身份恢复、数据库服务器本身损坏与文件系统恢复分别处理，不能扩大本节点验收范围。

## 2. 现有实现的边界

现有 `Job` 按 `(revision_id, kind)` 唯一，`CoreMaintenanceJob` 按修订和生命周期唯一，持久化清理块清单尚未携带工作区身份。直接重新使用这些任务索引新工作区会混入旧清理清单；旧删除任务还可能作用到新 Core。

`SourceRevision.indexed_at` 是内容/解析快照的时间。相同解析内容重新索引必须保留它，避免改变已有证据身份；物理重建时间应另行记录。

运行时只在一个事件循环中持有一个 Core。固定版 PostgreSQL 配置实际读取 `POSTGRES_WORKSPACE`，初始化日志将其称为 `PG_WORKSPACE`；该覆盖值的优先级高于构造参数。PGVector 不提供 model_name 时使用无模型后缀的旧表。必须显式绑定并核验实际存储工作区，且将目标向量存储身份与真实模型回调分开。

## 3. 身份与模型配置

### 3.1 Core 运行身份

`CoreIndexIdentity` 为不可变值：

- `workspace`：准确的逻辑工作区名，不做静默字符替换。
- `working_dir`：该代 Core 的独立工作目录。
- `vector_model_name`：旧索引为 `None`；新代为 `kg_` 加24位小写十六进制持久化随机 token，数据库保证唯一。

`LightRAGRuntime(settings, index_identity=...)` 仍使用 Settings 中的真实 Ollama 模型调用。token 仅决定上游向量表后缀，并非发送给 Ollama 的模型名。不得使用完整 SHA-256 作为 table suffix：上游不会截短，可能超过 PostgreSQL 的63字符标识符上限。固定版实际向量表前缀为 `LIGHTRAG_VDB_ENTITY`、`LIGHTRAG_VDB_RELATION`、`LIGHTRAG_VDB_CHUNKS`。24位 token 留出维度后缀空间；仍须对三种实际向量表名及配置维度分别检查长度。

初始化前明确设置实际配置键 `POSTGRES_WORKSPACE`，并同步兼容日志/操作说明使用的 `PG_WORKSPACE`；初始化后检查十二个存储的 `workspace` 全部等于持久化身份。缺失或不一致进入初始化失败/清理/要求重启流程，不报告 ready。

默认旧安装在 pin 之前使用固定版上游配置解析器读取有效覆盖值（环境变量优先，其次 `config.ini`）。与 `LIGHTRAG_WORKSPACE` 不一致时在数据库/模型/存储初始化前明确拒绝，提示使用原工作区同步配置。显式指定的目标身份才允许覆盖环境，不静默将旧安装切到另一个 namespace。

### 3.2 完整索引配置快照与分类指纹

持久化完整的规范配置和 SHA-256：provider、规范服务地址、model、维度、文档/查询前缀、实际解析器/分块配置以及可获得的模型版本摘要。密钥不进入指纹明文、API 或 Vault。LLM 信息单独记录，改变 LLM 不伪装成 Embedding 等价。

内部 `IndexProfile` 基础已实现：从实际 Core 和独立回调工厂捕获只读快照，分别计算内容/Embedding、图谱写入、LLM 和完整快照指纹。图谱写入指纹包含实际 extract LLM、采样、解析后的提示词、语言及摘要/成员限制；不能仅以 Embedding 指纹一致证明同一目标可续建。规范 JSON 含服务端解析后的提示词文本和服务地址，不向浏览器或 Vault 发布；公开信息使用 `public_summary()`。未知模型版本显式保留 unavailable。详见 [配置快照契约](index-profile.md)。

该模块当前仅支持本地解析后 `ainsert(rawtext)`、空 process_options、固定 tokenizer 与 legacy token chunker；不是保存对象后自动恢复 Core 的构造器。协调者接入时必须从封存配置构造真实回调/参数、验证外部实现和模型版本，并在初始化及写入前比较实际快照。代账本和事务冻结已作为内部基础实现；现有普通 Runtime 尚未接入，实际协调与重建执行器仍待实现。

先定义服务端独立 `llm`/`embedding` 角色配置和回调工厂：支持默认 Ollama、第三方服务及两者混用；实际实现文档/查询前缀，不只保存字段。目标从封存规范配置构造回调，密钥从服务端配置解析。规范配置包含实际生效的 parser 版本、tokenizer、分块 options、Embedding token 限制及上游环境默认项。重启时配置不匹配则保持冻结、报告需恢复的配置，不能用当前 Settings 的另一个模型继续同目标。

新代向量 token 在数据库中唯一，不能仅截取配置哈希后声称不会冲突。配置模型、维度或前缀改变时，普通索引和查询禁止继续使用既有代，要求显式重建；重启也不能将环境变量静默视为新索引配置。

旧版本未保存 Embedding 生产模型，只有同维度不能证明模型相同。兼容迁移须标记旧代配置为未核实；有现存索引的旧安装应通过显式全量重建进入已核实代。不能自动修改旧向量表身份或迁移旧向量后声称完成重新计算。空安装可在实际模型/维度校验后创建已核实初始代。

## 4. 持久化领域模型

使用专用重建操作和条目，不以复用普通索引 Job 代替重建日志。内部账本迁移为 `0013_core_generations`；后续生产接入仍需按本契约验收。

| 模型 | 必需字段和约束 |
| --- | --- |
| `CoreGeneration` | UUID；唯一 workspace、vector token；配置指纹及规范配置；独立 working 身份；配置状态 `legacy_unverified` / `sealed`；创建/激活/退役时间。配置封存不代表数据核验通过。 |
| `CoreSelector` | 单例 id=1；单调 CAS version；独立单调 execution_epoch；active generation；pending rebuild；冻结状态。初始化单例使用事务锁序列化。 |
| `RebuildOperation` | 稳定 UUID；旧/目标代；请求幂等键；预期 selector version；Vault binding 身份；状态、版本、快照摘要、错误、本次认领 token/fence、操作租约与时间。目标代只能属于这一操作。 |
| `RebuildItem` | operation/source 和 operation/revision 唯一；准确 revision/source 复合外键；生命周期、路径、原件哈希、原解析元数据快照；状态、attempts、本次认领 token/fence 和租约；目标清理块清单和解析结果。 |
| `CoreGenerationRevision` | generation/revision 唯一；写入意图及 indexing/failed/verified/cleaned 状态；实际解析哈希、持久清理块清单、物理索引和核验时间。Core 写入前登记意图，不能只登记核验成功的成员。 |

现有 `Job` 和 `CoreMaintenanceJob` 增加 generation 身份和明确退役记录，保留已有唯一约束和任务身份。现有 `SourceRevision` 保存当前派生索引所属代，内容和历史证据身份不改。

所有清理清单继承、claim、续租、完成、失败和重试必须按代筛选。旧代退役任务不能调用当前 Core；不能将“退役”写成 succeeded 来伪称旧索引已物理清理。

## 5. 协调与状态机

```text
queued → preparing → building → verifying → succeeded
                     ↘ failed ←─────────────┘
failed → preparing/building（显式重试，同目标代）
```

1. **接收**：短事务比较 selector version 和幂等键，创建目标代/操作，登记持久化冻结；返回202。状态请求不等待模型推理或持有长生命周期 runtime 锁。
2. **准备**：阻止新的普通模型/文件任务领取和来源写入；停止、等待并释放当前进程的问答、生成、文件、维护和索引任务。运行中的 Core 读取也必须退出。无法确认排空时保持 preparing/失败状态，不能同时启动第二个 Core。
3. **快照**：锁定 selector，按 UUID 顺序锁来源及其相关行。检查不存在未排空的 running 文件 I/O 或未释放的运行租约；queued 文件日志保留，不要求停止领取后它们自行消失。只捕获 active/latest，包括 queued/failed 的最新修订；活跃来源无 latest 等映射缺陷必须失败，不能静默跳过。保存全部快照条目和范围摘要，清空捕获来源的 current 指针。空范围是可验证的明确情况，不能用 checked=0 宣称任意损坏索引已恢复。
4. **切换运行对象**：先关闭新调用门禁并等待已进入的读写完成，不在排空时持有写锁。三个向量缓冲在模型队列关闭前落盘，因为懒计算仍需 Embedding。随后排空 role LLM 与独立 `embedding_func.func` 队列，对相同回调去重，核对实际队列/worker 数均归零。保存并关闭 parser executor，等待其线程实际退出；请求停止或清空 executor 字段不代表已停止。逐个捕获十二个存储 finalizer 结果，核对三个 vector pending buffer、每个 storage.db、保存的 pool 完全关闭（非仅正在关闭）以及 PG ClientManager 引用已释放；上游 `finalize_storages()` 会捕获失败，甚至有 vector buffer 未落盘但 client 已释放，因此正常返回或 ref_count=0 都不单独构成关闭证明。并发关闭共用保留的关闭任务；超时/调用者取消锁存失败，不撤销仍在清理的任务，也不因其稍后结束自动解除锁存。成功证明绑定该次 Core/epoch，新初始化后失效。全部确认后由协调者清理固定版模块级 shared storage 状态，再在相同事件循环创建目标 Core。持久化失败锁存属于应用协调者，不能通过替换 Runtime 对象清掉；不确定时保留状态并要求重启，拒绝再启动其他实例。
5. **目标预检**：首次初始化目标必须确认其所有 KV、向量、图谱、状态和缓存命名空间没有旧记录。不能依赖会吞掉错误的 `is_empty()`，也不能假设所有存储都有该方法。恢复操作允许自己的目标进度存在，不能误清空。
6. **构建**：专用条目执行器按准确原件哈希读取、重新解析、持久化该代写入意图、写入目标 Core 并验证。每个条目的租约包含操作/代/快照身份和本次认领 token/fence；续租和最终写入使用获得锁之后的数据库时钟，防止同 owner 再次认领的 ABA。
7. **核验与激活**：对全部快照来源再次核验原件和目标数据；在同一事务验证 active/latest/生命周期、条目结果、代和操作版本。全部通过才切换 active 代，更新 current、成员投影及最新普通 Job 的派生状态，清空 pending/freeze。然后开放普通任务。

重试保留同一个目标 workspace/token/working 身份。已完成条目重新核验后可跳过；中断条目按目标内自己的持久清单清理并重算，不继承旧代清单。部分成功不开放新问答。

目标核验需列明完整数据面：准确解析全文哈希、doc status、重新核对的文本块正文/ID/count/成员、块向量、实体/关系向量、图谱节点/边及完整成员锚点。无实体或关系的合法结果按准确空集合处理，不要求模型虚构节点。所有读取异常阻止激活，不能当作“不存在”。当前 `inspect_revision(...).healthy` 只涵盖 KV/status/已知块结构，必须扩展适配层的严格目标验证，不能直接拿它证明向量/图谱健康。

严格审计适配层先 flush，再直接读取已持久化数据库记录，不能调用会吞 SQL 异常、从内存 buffer 返回或现场重新生成 Embedding 的上游宽松 getter 来证明落盘。采用两级可执行谓词：

- **条目**：按封存解析/分块参数复算全文、块正文/ID/顺序/数量和归属，比较落盘记录；向量存在、维度正确、数值有限、索引文本一致。
- **工作区**：doc/status/chunk/vector 集合与全部快照条目的预期集合准确相等；实体和关系 anchors、完整 chunk tracking、图节点/边/端点集合相互一致，拒绝额外或孤儿记录。完整归属使用 `entity_chunks`/`relation_chunks`；graph/vector 的受限 `source_id` 投影可能截断，应验证它符合固定版投影规则，不能要求它与完整 tracking 集合相等。空集合由严格全局读取证明。

结构健康验证不重新调用 LLM，也不重新计算所有向量；生成事实的语义正确率仍由 M6 标注评估和人工审阅验收。

## 6. 锁、围栏与现有任务整合

- 统一锁顺序：selector → operation → source（UUID排序）→ 普通 Job → maintenance → file operation → revision → rebuild item/member。禁止反向获取 selector；普通写入在锁 source 之前获取短期 selector 围栏。
- Freeze 的检查需要和来源写入共享事务围栏，不能仅检查浏览器请求到达时的内存布尔值。
- 准备阶段仅允许已持有旧代租约的任务排空；快照封存后旧租约不能更新来源派生状态。新 claim 和请求始终拒绝。
- selector 的请求 CAS version 与执行 execution_epoch 分开：接收重建改变 version，但 preparing 仍允许既有有效 grant 排空；快照封存后递增 execution_epoch，使旧 grant 全部失效。不能用 selector.version 相等作为唯一执行资格。普通短事务先取得 selector `FOR SHARE`，重建转换使用 `FOR UPDATE`；`FOR KEY SHARE` 不足以阻止冻结字段更新。
- Core 方法需要调用入口与关闭排空保护，覆盖 HTTP 实体导航等直接读取，不能只停止后台任务后立即释放存储。
- 启动顺序为应用库 → selector/pending/config → Vault → 选定唯一 Core → 对应执行器。pending 存在时只恢复目标操作，不先启动普通 file/model/reconciliation runner；`retry-initialize` 和 Vault 切换同样受冻结围栏。重建接收入口不要求损坏旧 Core ready；旧部分初始化无法确认释放时，持久化操作后要求进程重启。激活提交前预装目标服务引用，提交后再开放入口；旧/目标引用不能由不完整的 `_install_vault` 更换代替。
- 激活保留来源生命周期、修订 UUID 和原件 SHA。解析文本和定位契约未变时保留 `indexed_at` 与证据；解析文本或 segment 定位契约改变时显式使旧证据失去当前资格，历史证据仍保留，不静默重写已发布定位。
- 普通 Job 的当前代清理清单替换为目标核验结果，而不是与旧代合并。旧历史 Job/maintenance 明确退役，不在新代运行。
- 新的来源删除清理所有可能写入 active 代的修订，包括 indexing/failed 的写入意图，不能只检查已核验成员。Trash 前置条件依据“从未分配该代写入”或“该代的当期清理及严格不存在检查确实完成”。仅没有 verified member 不能证明 Core 没有部分写入；旧代退役不算清理成功，旧代未激活数据不阻止权威原件归档。
- 恢复来源即使旧修订标记 ready，只要它不属于 active 代也必须重新索引，current=None。
- 新问答、生成、实体/关系导航的资格同时要求 active 代成员已核验；旧历史读取继续显示准确证据和重新计算的 freshness。
- 围栏覆盖检索及最终发布事务，包括 `GenerationRepository._assert_current_evidence`（问答/审阅也复用）、来源/维护/文件日志、实体导航和 Wiki 发布。先定义共享代资格谓词与完整 ExecutionGrant，再传递至各入口，不能只修改 provenance 仓储。运行器调用 Core 前还要核验 grant 的代与实际挂载 Core 一致。

## 7. API 和 Web 契约

| 接口 | 行为 |
| --- | --- |
| `GET /api/v1/system/index-state` | active/target 代、配置匹配、是否冻结、最近操作、可操作原因；不暴露密钥。 |
| `POST /api/v1/system/rebuilds` | 请求包含幂等 operation_id、expected selector version 和目标配置摘要。来源范围由服务器捕获；不接受任意路径或上游 Core 对象。返回202操作。 |
| `GET /api/v1/system/rebuilds/{id}` | 阶段、范围计数、完成/失败/等待数、当前条目、错误与更新时间。条目采用有界分页。 |
| `POST /api/v1/system/rebuilds/{id}/retry` | 要求操作版本和同一目标配置；幂等重复不创建新 workspace。 |

冻结时来源上传/更新/删除/恢复、普通重索引、模型生成/问答的新请求和审阅应用返回明确409或503，并附操作 ID；Wiki/历史/准确原件读取仍可用。Wiki 的手工编辑能否在重建期开放须结合现有审阅日志围栏验证；不能影响目标源快照或绕过审阅冲突检查。

Web 展示实际阶段和计数，不提供虚假百分比。失败时提供修复原因与“继续此重建”，不自动删除旧数据、清空数据库或从头生成目标。浏览器断开后任务继续；刷新从数据库恢复显示。

## 8. 必须的验证

### 8.1 自动化

- 真实 PostgreSQL：并发创建只接受一个冻结操作；过期 selector/operation version、重复请求和跨操作条目拒绝。
- 快照：只含 active latest；删除/更新/上传与 freeze 的竞争不改变封存范围；原件不存在/哈希变化无法激活。
- 租约：Source/selector 锁等待后的过期续租/完成失败；过期操作/条目恢复；旧代任务不能作用到目标；取消后先 drain 再释放所有权。
- ABA：同 owner 在 lease 到期后重新认领，旧 token/fence 的续租、清单、完成和失败全部拒绝。
- 失败恢复：写入后、条目完成前、全部完成后、激活事务前后分别注入中断；重启仍是同一目标，重复激活不改身份。
- 保留性：原件/Wiki/证据哈希、来源/审阅身份及同解析内容 indexed_at 不变。
- 代资格：旧 revision ready 不代表当前；恢复排队 target；清理清单不跨代；Trash 对代的前置条件准确。
- 部分写入：普通索引写 Core 后失败且没有 verified member，删除仍清理该代残留；不能因为未核验就跳过清理和归档前置检查。
- Runtime：真实固定版构造参数、实际 storage workspace、向量表名长度及模型 token；读操作与关闭竞争；旧实例未释放时禁止新实例。
- 前端：API契约、真实状态轮询、重试幂等、来源动作冻结与历史读取；生产构建。

### 8.2 实际本地验收

在已有隔离恢复副本上备份后进行，不改用户私有 Vault 或原8787实例：

1. 保存原件、已审阅 Wiki 和证据清单与应用身份。
2. 在旧工作区注入至少文档和向量/图谱数据不一致；证明旧索引不足以通过目标验证。
3. 从 Web 发起完整重建。记录 fresh target、实际模型与每个 active latest 结果；证明删除/历史来源未加入。
4. 在存在已完成与未完成条目时中断进程，重启并完成同一目标。需要多条活跃来源才能证明部分进度保留，不能只有一个已完成来源。
5. 完成后真实新问答返回受原文支持的声明，引用 current/latest；Wiki ↔ 实体导航恢复，原件和审阅文件哈希保持一致。
6. 检查旧数据仍保留、目标无已删除/历史成员、重启不重新生成工作区；确认一次只存在一个活动 Core。

该验收仍不替代 M6 的30题人工评估、100%引用可解析、新机器安装和发布验证。

## 9. 文件所有权与推进顺序

Sol 维护本契约、跨模块接口和最终集成。

1. Luna A：Core 身份与存储工作区核验（`index_identity.py`、`config.py`、`lightrag_runtime.py`、相邻测试）。该基础不提供重建按钮。
2. Luna B：服务端独立角色配置和真实回调工厂、前缀行为与规范配置指纹；Core 文件在 A 交接后串行修改。
3. Luna C：Core 调用门禁、全模型队列和存储关闭证明、唯一对象的恢复启动接口；这些接口先于执行器确定。
4. 契约审查通过后 Luna D：代/操作/条目/member 模型和迁移、重建仓储与普通来源/维护/文件/证据的代围栏及 PostgreSQL 测试。共享 models/schema 和同仓储文件串行处理，写入意图与归档规则在重建开放前一起落地。
5. Luna E：固定版严格审计适配层、专用执行器与应用生命周期整合；与 A/C 的 Core 文件交接后再写。
6. 后端接口稳定后 Luna F：Web 操作与进度、相邻类型和契约测试。

每个可验收节点检查实际 diff 和风险相关测试，独立审查后提交并尝试推送。若只完成内部基础，状态须明确为基础完成、完整用户流程待验收。

2026-10-06 architect 确认 D 的实现边界：先实现尚未被生产 Runtime 使用的完整账本、迁移、仓储、事务围栏和真实 PostgreSQL 检查；不得在局部围栏完成时启用 selector 或重建入口。首个运行时接入节点必须同时覆盖普通索引/维护/文件/证据 grant、写入意图、归档/恢复规则和 selector-aware 启动。旧索引回填为 legacy_unverified，不凭 ready/Job succeeded 生成 verified member；相同文本但页/heading/segment 定位变化必须使当前证据失效。
