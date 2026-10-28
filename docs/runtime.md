# 本地模型运行契约

## 模式与环境变量

所有真实调用都使用 `local` 模式；`SyntheticProvider` 必须明确选择，错误时绝不自动切换。启动先输出 provider 指纹和模式；`/api/health` 仅证明 HTTP 进程可响应，不调用模型。

| 配置 | 默认/含义 |
| --- | --- |
| `QA_ROOT` | `.quality-data`；推荐显式改为忽略的 `runtime/` 子目录 |
| `QA_PROVIDER` | `local`；另一个值仅为显式测试 `synthetic` |
| `QA_RUNTIME_CONFIG` | 未设置；设置后使用 ManagedLocalProvider，本地绝对路径配置 |
| `QA_GENERATION_URL` | `http://127.0.0.1:18080` |
| `QA_EMBEDDING_URL` | `http://127.0.0.1:18081` |
| `QA_RERANK_URL` | `http://127.0.0.1:18082` |
| `QA_GENERATION_MODEL` | `qwen3-4b-instruct-2507` |
| `QA_EMBEDDING_MODEL` | `bge-m3` |
| `QA_RERANK_MODEL` | `bge-reranker-v2-m3` |
| `QA_TOKEN` | 可选 CLI Bearer 认证；不写入配置示例、不放命令行参数 |

`QA_RUNTIME_CONFIG` 只在 `local` 时生效；此时 URL/模型 ID 来自托管配置与固定别名，而非后三组 HTTP 环境变量。`--root`、`--provider` 可覆盖相应默认值。

## 方案 A：已启动的本机服务

不设置 `QA_RUNTIME_CONFIG` 时，`LocalProvider` 只访问操作者明确配置的数字回环 HTTP URL，不接受 `localhost` 主机名、远端地址、凭据、query 或 fragment。默认三个端口分别承载生成/嵌入/重排；无环境代理、无重定向、无云端回退、无自动推理重试。

对接协议：

- `POST /v1/chat/completions`：`response_format.type=json_schema`、温度 0；返回可校验的严格 JSON、正整数 `usage.total_tokens`，不能以截断结果充当完整计划。计划必须显式返回 SQ 分组；假设/反思按工具类型限制参数，SQL 的分组必须等于当前 SQ。模型 SQL 只允许字面量，不提供独立 `parameters` 字典（显式空字典也拒绝）；底层 Verifier 仍保留严格命名绑定能力。程序仍独立执行来源、分组 AST 和只读查询校验。
- `POST /v1/embeddings`：浮点嵌入，`data[].index` 完整且可恢复输入顺序；索引检查数量、有限数值和一致维度。
- `POST /reranking`：返回 `results[].index/relevance_score`，覆盖全部输入文段。分数不是概率。

这种连接模式按模型别名和协议构造指纹，**不验证远端进程所加载权重的文件哈希**。不能把返回相同别名当作二进制或模型身份的密码学证明。

## 方案 B：ManagedLocalProvider

`QA_RUNTIME_CONFIG` 指向操作者本机准备的 JSON；不通过 API 上传，不提交仓库，不附带真实文件路径示例。可使用仓库脚本生成配置，脚本只读取操作者明确指定的本机引擎/模型文件并计算哈希，不下载或启动服务。**生成目标必须是仓库之外的新文件**；下列变量由操作者设置，不在文档给出本机私有路径：

```bash
.venv/bin/python scripts/configure_runtime.py \
  --binary "$LLAMA_SERVER" --models-dir "$MODEL_DIR" \
  --lock-dir "$RUNTIME_LOCK_DIR" --output "$QA_RUNTIME_CONFIG" --port 18080
```

该脚本要求指定三份固定文件名/大小/哈希的权重，以及 binary 同目录的 `.dylib`/`.so`；不接受替换模型来冒充固定验收组合。已实际运行该命令，成功验证三份权重并生成仓库外的新配置，没有启动模型进程。

配置键严格校验，不接受任意命令或 shell 脚本：

| 字段 | 结构 |
| --- | --- |
| `binary` | `{path, sha256}`；实际 llama-server 文件，SHA-256 为 64 位小写十六进制 |
| `libraries` | 1–30 个 `{path, sha256}`；依赖库必须在 binary 同一目录 |
| `models` | 恰好 `generation`、`embedding`、`rerank`；每个为 `{path, sha256, bytes}`，bytes 为真实正整数大小 |
| `lock_path` | 已有父目录下的本地锁文件路径 |
| `port` | 默认 `18080`，允许 1024–65535；三种模型依次共享同一端口 |

全部运行路径必须绝对、彼此不同；不要填示例零哈希。首次使用以及文件身份变化后核验二进制、依赖库与当前模型的 SHA-256，模型还核验字节大小；拒绝不符合要求的文件与直接符号链接。模型指纹绑定配置哈希、别名、协议和上下文设置。配置是可信本地操作者输入，不代表对恶意本机管理员的防御。

运行方式示意（变量由操作者在会话中安全设置，仓库没有该配置文件）：

```bash
.venv/bin/quality --root runtime/local-workbench --provider local serve

# 独立的新目录，显式发起真实模型流程；耗时可能明显高于测试桩。
.venv/bin/quality --root runtime/local-demo --provider local demo
```

托管运行机制：

- 每次请求取得线程锁及本机 OS 文件锁；一次只驻留一个模型，请求结束即停止该子进程。串行设计降低内存驻留量，但模型反复加载会增加延迟。
- CPU 是显式配置：`--n-gpu-layers 0 --device none --no-op-offload --flash-attn off`，不是运行失败后静默回退。
- 生成上下文 8192；嵌入/重排上下文 2048；并行度 1、CPU 线程 4。BGE-M3 使用 `cls` 池化，重排使用 `rank` 池化。
- 参数列表直接交给 `subprocess.Popen`，不用 shell。子进程采用临时 HOME/TMPDIR、最小环境，不继承云 token/代理；仅监听 `127.0.0.1`，关闭自带 web UI、slot 页面及日志输出。
- 拒绝连接已占用的模型端口，避免把未知进程当作目标引擎。加载健康等待最多 90 秒；请求 HTTP 超时按 provider 契约处理。未知结果进入人工对账，不自动重发。
- 使用 `fcntl` 文件锁，当前实现面向 POSIX；未提供 Windows 托管运行兼容性承诺。

## 指定运行组合与验证状态

| 组件 | 指定版本/精度 | 当前文档可主张的事实 |
| --- | --- | --- |
| llama.cpp | `b6500`，提交 `a7a98e0fffed794396b3fbad4dcdbbc184963645` | 固定源文件身份核对后构建成功，配置记录实际二进制和库哈希 |
| Qwen3-4B-Instruct-2507 | `Q4_K_M` | CPU 实际意图、计划、假设/SQL smoke 通过；完整调查另见验收记录 |
| BGE-M3 | `FP16`，dense 1024 维 | 实际中文向量与小型独立评估通过；不实现 sparse/ColBERT |
| BGE-reranker-v2-m3 | `FP16` | 实际重排通过；输出按原索引对齐，分数不是真实性概率 |

本次运行环境是 Apple M1、16 GiB、macOS 13.2、Python 3.12。Metal 路径两次在模型预热时 SIGSEGV（包括关闭 flash attention 后），**GPU 路径未验收通过**。随后显式选择同一权重的 CPU 配置，完成真实推理。不是模型、云端或测试桩自动回退，也不承诺其他平台已通过。

实际配置、原始失败记录及运行日志保留在仓库之外。详细步骤和最终范围见[验收记录](validation.md)；公开文件来源及模型许可证边界见[依赖说明](dependencies.md)。

## 故障处理

- `runtime_config`：检查 JSON schema、绝对路径、锁目录和权限；不要把完整配置贴进公开 issue。
- `runtime_integrity`：本机文件大小/哈希/身份不匹配；停止，核对受信来源。不要改成跳过校验。
- `provider_unavailable`：端口占用、加载失败、忙或无法连接；排查后通过业务显式恢复入口操作。
- `provider_http`：已知协议/请求错误；修复协议配置，不转换成成功。
- `provider_unknown`：不能判定是否完成，必须先对账；不承诺调用 exactly-once。
- `index_provider_mismatch`：当前可见报告需使用当前模型重新入库；不能把另一模型的向量当作兼容索引。
- `snapshot_changed`：数据/索引/权限/模型变化；旧确认、引用、校准或回执不得绕过检查。
