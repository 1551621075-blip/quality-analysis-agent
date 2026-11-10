# 质量分析 Agent（本地 Demo／测试版）

本仓库提供本项目的本地 Demo／测试版本，用于生产部署前在本地验证核心功能、业务流程与交互方式。

基于原创合成数据开展质量调查：**一个 Agent、13 个 LangGraph 节点，不是 13 个独立 Agent**。模型提出结构化计划和假设，程序执行有授权边界的取证与统计，人工确认后才执行子问题（SQ）。

**使用范围：** 本版本使用合成数据与本地测试环境，不包含真实业务或设备数据、内部资料、生产模型及敏感配置。本地验证结果不代表生产环境的可用性、效果或业务收益。

**已完成本地 Demo 验收：435 项测试、20 项真实浏览器检查、5 项真实模型检查，以及真实 CPU Qwen/BGE 的核心分析与授权追问全流程均通过。** 数据和历史报告均为原创合成材料，不把测试桩、历史版本或部分步骤冒充当前真实模型结果。范围、开发失败记录和限制见[验收记录](docs/validation.md)。不是生产系统，不提供因果证明，不执行生产处置。

## 快速开始：明确使用测试桩

要求 Python `>=3.12,<3.13`；LangGraph 固定 `1.2.5`。以下命令在仓库根目录执行。

若尚未准备依赖，由操作者在获准联网安装后显式执行 `uv sync --locked --python 3.12`。该命令会安装依赖；本文的验收脚本不会代为安装或下载。锁文件记录公开镜像来源和哈希；已有环境直接使用 `.venv/bin/python`、`.venv/bin/quality`。

```bash
# 仅确定性 SyntheticProvider，不调用 Qwen/BGE；要求新的、空的数据目录。
.venv/bin/quality --root runtime/synthetic-demo --provider synthetic demo

# 用不同的新目录保留可交互演示数据。
.venv/bin/quality --root runtime/workbench user demo-admin --role admin --plant PLANT-A
```

`user` 是受本机文件权限保护的初始化命令，无远程开户 API；它只显示一次随机令牌，服务端仅保存哈希。保管输出，不把令牌写入代码、命令参数、URL、报告或截图。

```bash
# 交互终端会无回显询问 admin 令牌。
.venv/bin/quality --root runtime/workbench --provider synthetic seed
.venv/bin/quality --root runtime/workbench --provider synthetic serve --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`，用该用户的令牌登录。另一个终端可以通过 `user` 创建 `operator`、`analyst`，但他们只看到本人会话及当前 ACL 允许的报告。不要暴露到公网；`serve` 拒绝非数字回环 IP。

## 真实本地模型：显式 local，无自动回退

- 默认 `QA_PROVIDER=local`；`--provider synthetic` / `QA_PROVIDER=synthetic` 仅用于明确的测试。
- 提供 `QA_RUNTIME_CONFIG` 时，使用 `ManagedLocalProvider`：本地 JSON 指向操作者准备的二进制、依赖库和模型文件，校验 SHA-256；CPU、一次一个子进程、无 shell、无云端回退。不会下载模型。
- 不提供 `QA_RUNTIME_CONFIG` 时，连接已由操作者启动的本地 HTTP 服务：`127.0.0.1:18080` 生成、`:18081` 嵌入、`:18082` 重排，模型别名全部小写。
- 指定运行组合为 llama.cpp `b6500`（`a7a98e0fffed794396b3fbad4dcdbbc184963645`）、Qwen3-4B-Instruct-2507 `Q4_K_M`、BGE-M3 `FP16` dense `1024`、BGE-reranker-v2-m3 `FP16`。配置存在和哈希匹配不等于推理验收完成。

完整配置、协议与限制见[本地运行](docs/runtime.md)，逐步 CLI/API 操作见[使用指南](docs/usage.md)。

## 必须保留的工程边界

- 首轮仅执行人工选择的 `core` SQ；追问明确选择 SQ 并包含其依赖，生成新轮次证据。准备阶段可以读取 cohort 元数据和先验报告，并非“确认前零读取”。
- SQL 只在程序裁剪的工厂、产品、日期、产线范围内执行；每个 SQ 明确分组维度，程序同时检查工具声明与 SQL 分组，不能顺带换维度分析。模型不能执行任意 Python、写业务库或扩大权限。
- 历史报告的召回与原文下钻受工厂及 actor ACL 控制，**当前没有报告产品/日期过滤**。先验不是当前批次证据，也不是因果证明。
- 入库需要人工提供 `phenomenon/context/hypothesis/evidence/action` 五摘要。摘要用于检索入口，引用落在原文切片。报告不用于 LLM 训练或微调。
- 推理、工具、请求、人工动作与恢复分别记账；未知推理结果停止并要求人工对账，不承诺外部 provider 的 exactly-once。
- 缓存只复用精确范围、身份/ACL、数据/索引/模型版本一致的先验；不把旧轮次 Evidence 当成新证据。
- 输出数字来自程序证据；区间、分组和关联不是根因结论。建议需要人工审核，不连接生产执行系统。

详见[架构与安全边界](docs/architecture.md)、[依赖与许可证边界](docs/dependencies.md)。本仓库**未授予整体开源许可证**；上游组件各自的许可证不能自动授予本仓库、模型或数据的整体再分发权。

## 本地质量与发布检查

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/check_wheel.py --wheel dist/quality_analysis_agent-0.1.0-py3-none-any.whl
```

最后一条要求操作者已构建该 wheel；也可以显式选择 `--build`，仅当 uv 与构建依赖已就绪时离线构建。缺失依赖直接失败，不自动安装、不访问外部下载源。检查临时 wheel 安装布局、RECORD、入口点及静态资源时隔离源码导入，不启动真实模型。发布步骤、扫描 allowlist 和“未验证”项目见[验收记录](docs/validation.md)。
