# 独立模型角色适配层

本节点提供内部可调用的角色配置与真实异步 HTTP 回调。默认应用启动仍使用既有 Ollama 路径；Web 模型设置、持久化索引代和配置匹配围栏尚未接入，不能通过新增环境变量直接切换已有索引。

## 两个独立角色

`src/knowgrain/providers/` 定义 `LLMRoleConfig`、`EmbeddingRoleConfig` 与 `build_provider_callbacks`。LLM 与 Embedding 分别指定 provider、服务地址、模型、超时和凭据，可以使用不同服务。`role_configs_from_settings` 仅转换当前已有的本机 Ollama Settings，不读取环境中的云服务密钥。

| 角色 | Ollama 原生协议 | OpenAI-compatible 协议 |
| --- | --- | --- |
| LLM | 配置地址 + `/api/chat` | 配置地址 + `/chat/completions` |
| Embedding | 配置地址 + `/api/embed` | 配置地址 + `/embeddings` |

服务地址需显式指定。例如兼容接口地址含 `/v1` 时，该部署路径被保留；不会自动添加默认云服务地址。HTTP(S) 地址拒绝内嵌用户凭据、查询参数、fragment 和控制字符。请求禁止跟随重定向或使用环境代理。每次调用独立创建并关闭有限超时的 HTTP 客户端，取消会传播，不在回调内自动重试。

兼容协议遵循 [Chat Completions 请求定义](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 与 [Embeddings 请求定义](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。兼容协议支持不代表每家服务都支持同一模型、JSON Schema 或可选参数；不支持时保留明确失败，不自动换服务。

## 输出约束

- LLM 仅支持当前流程需要的非流式文本输出。系统提示、历史和当前提示按顺序传输，不修改调用者的历史对象。支持 JSON object/JSON Schema 格式映射；截断、工具调用及空输出不能当作成功文本。
- Core 内部缓存、优先级、token tracker 等对象不进入 HTTP 请求。调用参数不能覆盖已配置的模型、服务地址或凭据；未支持的参数明确拒绝。
- Embedding 严格返回 `(输入条数, 配置维度)` 的连续数值数组。拒绝布尔值、字符串、非有限值、缺失或不完整向量。兼容接口按返回 `index` 恢复输入顺序，重复、越界和缺失索引失败。
- 文档与查询前缀按 `context` 分别拼接，保留配置的空格；Core 包装时必须使用 `EmbeddingFunc(supports_asymmetric=True)`，否则上游会移除 context。
- `dimension` 用于验证返回值；`send_dimensions` 单独决定是否向兼容接口请求该维度，默认不发送。不会悄悄裁剪或补齐向量。Ollama 请求明确禁止自动截断输入。

## 封存与凭据

角色配置不可修改，公开规范投影不包含 `api_key`。规范 JSON 使用固定键顺序，Embedding 角色指纹覆盖服务、模型、维度、前缀及角色选项；LLM 元数据单独导出。凭据轮换不改变该角色指纹。

`embedding_role_fingerprint` **只是角色指纹**，不能证明整个索引配置相同。完整索引代还需封存实际 parser、tokenizer、chunker、生效环境默认项及可获得的模型版本信息，并在重启和重建时检查匹配。存储向量 token 也不是调用的真实模型名。

配置密钥只供服务端传输使用，不进入规范投影、Vault、浏览器或错误文本。适配错误只包含角色、provider、安全错误类别和可获得的 HTTP 状态码，不输出模型响应、用户提示、密钥、URL 或原始异常字符串。

## 后续集成

完成实际 Core profile 封存、持久化代账本与配置匹配围栏后，应用才能使用这些回调处理已有资料。更换 Embedding 服务、模型、维度或前缀时必须显式重建；同维度不意味着新旧向量可以混用。完整执行顺序见 [工作区重建契约](m5-workspace-rebuild-contract.md)。
