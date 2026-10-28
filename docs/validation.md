# 验收记录与发布闸门

本记录区分确定性测试、真实模型推理、浏览器操作和标准发行物安装。数据与报告均为本仓库原创合成材料；**没有生产、因果、企业收益或多平台验收**。当前 v10 已通过下列本地验收，包括真实完整调查；不能把这个结论外推到其他模型、数据或生产系统。

## 已完成的检查

| 检查 | 实际结果 | 适用范围 |
| --- | --- | --- |
| 全量 pytest | 435 passed，42.04 秒 | v10 全部证据、查询、文档适配器修复后的源码 |
| 静态检查 | `src/tests/scripts` Ruff、工作台 JS 语法均通过 | v10 当前源码 |
| SQ 分组专项 | 包含在全量内 | 核心/反思越维度、条件聚合、投影/HAVING/ORDER、原始记录、错误指标拒绝；metadata 不能替代 SQL |
| 模型/验收契约 | 包含在全量内 | 显式分组、工具专属字段、异常 envelope、缺样本/错统计拒绝；正式报告禁止原始 SQL 投影 |
| 图恢复专项 | 80 项 SQ/图/恢复组合通过（11.81 秒），已再次包含在全量内 | 修复回执使用真实 SQL；故障注入不冒充进程 kill 全覆盖 |
| 引用专项 | 已包含在全量内；实际 4096 条签发容量场景此前通过 | 新增 14 项真实文档适配器回归：精确引用、来源区分、完整报告及无权/过期/伪造拒绝；非生产压力测试 |
| 浏览器 | Chrome 155.0.8059.85，20 pass / 0 fail，8.15 秒 | v10 文档修复后真实 DOM、回环 API，显式 SyntheticProvider |
| 真实本地模型 smoke v10 | 5 / 5 通过 | CPU Qwen 意图/计划/假设与 SQL 执行、BGE 中文向量、reranker；固定开发用例 |
| 真实完整调查 v10 | 全部通过，369.130 秒（含图外入库/校准） | core 产线比较、单独授权材料追问、6200 条观察/2 产线/8 批次、新旧 Evidence 分离、正式报告无原始投影 |
| 真实 BGE 中文校准 | fit 6 条、独立 eval 6 条均符合原标签 | 极小原创 fixture，详细口径见下节；非生产检索率 |
| 运行时配置助手 | 已用实际引擎/库及三份权重运行通过 | SHA-256 与字节数核验，生成新配置，不启动服务 |
| sdist/wheel | v10 构建、归档/RECORD/入口/资源/源码隔离通过；sdist 在仓库外离线重建出相同 wheel 文件内容（2.423 秒） | 47 个公共文件均进入 sdist，额外只有 PKG-INFO；验收记录最终修改后按相同命令再重建检查 |
| 标准安装器 | 新 venv，离线锁定依赖 + 实际 v10 wheel 安装通过，13.976 秒 | `uv sync --frozen --no-install-project`、`uv pip install --no-deps`；非解包冒充安装 |
| 安装后运行 | 依赖一致性、CLI 入口、源码外 API/资源、完整 synthetic demo 通过 | core 与 followup 均 completed；没有真实模型调用 |

没有把重叠专项计数相加。构建后的最终归档仍须与最终源码逐字节一致；上传时另核对新私有仓库、提交及所有 blob，不能用本地测试代替远端核验。

### 浏览器覆盖与遗漏

已覆盖三角色登录及操作限制、显式 Scope、准备不执行 SQ、空选择不发送确认、core 确认、extended 独立授权、新轮次/新 Evidence、报告/Revision/审计、原创报告入库/搜索/原文下钻、字面 XSS 文本、他人会话 404、跨工厂拒绝、ACL 修改/删除使旧引用和报告失效、身份变更/撤销/退出/重载清空、实际 no-store。

浏览器和服务使用新临时目录，不读取现有浏览器 profile。结束后扫描 249 个自建临时文件，无原始或 UTF-16 令牌残留；本地存储/cookie/IndexedDB/CacheStorage 为空，无令牌外发或控制台泄漏，子进程与临时目录清理通过。

脚本标记的 3 类未测事项包含多项行为：真实模型/生产；其他指标及日期/产品/产线范围、上传、校准、重试/对账和历史版本面板；人为延迟响应下的身份切换竞争。不要把这些遗漏写成已通过。

### 恢复契约

状态版本与 worker lease 阻止迟到执行者覆盖新轮次；工具预算在调用前落盘，结果、Evidence 与回执在同一事务提交，重放使用原身份；反思保存原失败上下文；人工动作绑定具体 round 的结束快照，不拿后续 round 冒充原回执。

没有持久化工具结果时返回 `tool_unknown`，禁止自动重试，需新建分析。未知生成调用必须由本人且 admin 核查真实回执或明确无结果，再显式恢复。旧版本缺少轮次绑定的动作返回 `action_unrecoverable`，不猜测。测试使用故障注入，不声称外部推理 exactly-once 或所有 OS 崩溃窗口已验证。

## 真实模型结果

固定组合：llama.cpp b6500、Qwen3-4B-Instruct-2507 Q4_K_M、BGE-M3 FP16 dense 1024、BGE-reranker-v2-m3 FP16。公开来源及权重哈希见[依赖说明](dependencies.md)。Apple M1 / 16 GiB / macOS 13.2 上显式 CPU 运行；Metal 预热两次崩溃，GPU 未通过。一次驻留一个模型，每请求重新加载，延迟包含启动开销，不是吞吐 SLA。

### 当前开发 smoke v10

| 检查 | 用时 / 用量 | 实际检查 |
| --- | --- | --- |
| 意图 | 7.722 秒 / 112 tokens | 严格 schema，返回 quality_analysis |
| 计划 | 24.544 秒 / 478 tokens | core 产线 + extended 材料等维度、有效依赖 DAG；列入计划不等于授权执行 |
| 假设和程序取证 | 25.046 秒 / 505 tokens | 当前 SQ 分组为 line_id；生成 SQL 经独立指标/分组闸门和真实 SQLite Verifier 执行，status=ok |
| 中文嵌入 | 1.406 秒 | 1024 维、有限值、L2 约为 1；相关 0.778274、无关 0.341680、重复约为 1 |
| 重排 | 1.288 秒 | 正确相关项 index=1 得最高分 3.53081，恢复输入顺序；logit 不是概率 |

SQL 结果：两条合成产线各 3100 条观察，分别 155 / 3100 和 780 / 3100 缺陷。程序独立重算分子、分母、Wilson 区间；不是仅验证 JSON 格式。假设中的“显著差异”等模型措辞不是统计显著性结论，renderer 不把它当成已证实根因；当前不做显著性检验。

这些是反复开发的契约用例，不是未见测试集。旧失败记录全部保留在仓库外：v1 有越出 SQ 来源的工具；v2 仅结构检查 5/5，但 SQL 使用不存在的 inspection_date，不能算业务 SQL 通过；v3 增加精确字段提示和实际 SQL 执行检查后通过。v5 的更严格检查中，意图、计划、嵌入、重排通过，假设 SQL 被参数绑定检查拒绝（4/5）：后续单项诊断保留了实际输出，模型把 scope 配置复制进 `parameters`，却没有 SQL 占位符。程序没有删除这些错误参数来伪装成功；v6 在输出字段和提示中明确绑定规则，仍由 Verifier 检查精确键集合。另有独立 CPU smoke 状态分类未达预期、独立 rerank 测试误以为响应已按相关性排序；都没有改写失败记录。

### 中文检索校准

8 份原创双语报告真实 BGE 建索引。12 个中文查询分 fit/eval 各 6 个，每组 4 个正例、2 个无答案。两组正标签文档不相交、查询唯一。仅 fit 选择阈值和 top-k，然后评估 eval；没有用 eval 调参。

本次 fit/eval 均为 precision=1、recall=1、F1=1、无答案拒绝率=1，各 TP=4、FP=0、FN=0。**每组只有 6 个样例，这不是稳健的生产性能估计**。原始记录含案例/配置哈希、provider、actor/ACL 和 index revision，保留在本地而非提交数据库。

英文与中文 fixture 使用相同文档划分，不是跨语言独立数据集。API 的 `use_original_cases` 默认英文，完整真实验收脚本明确中文。索引、身份/ACL 或模型变化会使旧校准无效；安装是显式动作。

### 完整生成调查

`real_model_analysis.py` 使用真实 BGE 校准后，调用真实 Qwen 准备计划、人工确认 core、明确授权 extended 及依赖，要求新旧轮次 Evidence 分离。验收脚本按固定原创数据的独立 oracle 检查全部 6200 条观察、两条产线与八个材料批次的确切分子/分母，不允许只有元数据、漏样本或错误分组冒充完成；这组 oracle 不发送给模型。首次完整运行耗时 556.901 秒（包括图外入库/校准），**失败**：图内累计 405.48 秒时，后续 verifier 因 300 秒累计预算停止，没有放宽预算。

复核输出还发现核心“产线比较”子问题附带了材料批次和班次查询：SQL 都合法、统计可核对，但程序当时只约束工具来源，没有约束 SQ 分组维度，因此这份核心报告也不能作为授权边界验收成功。随后增加显式 `Subquestion.group_by`、计划中展示的授权维度和执行前 SQL 分组校验，并减少重复提示/冗余输出；全量 321 项回归通过。

第二次完整运行使用 v4 协议，耗时 323.095 秒（含图外入库/校准），仍然**失败**。核心计划误将产线对比写成 null 总体分组；SQL 工具请求还混入 `document_id`，程序以 `invalid_tool_request` 拒绝，Reflection 没有修复，核心报告为空且 unresolved。原始输出和失败结果保留。后续修改按工具分别限定输出字段、要求显式 SQL 分组并在 provider 返回处复核，验收脚本改为人工确认前先核对计划维度；不是静默删除错误字段或执行错误计划。

第三次完整运行使用 v8 协议，耗时 251.952 秒（含图外入库/校准），在确认前**失败**：模型标题是“各产线缺陷率总体对比”，但 `group_by=null`。独立计划验收拒绝了它，没有执行错误核心 SQL。随后复核实际调用上下文发现，服务与字段描述仍使用含混的“null 代表总体”，模型 schema 还带 `default:null`。v9 将分组输出改为无默认值的显式枚举，并统一说明“比较各组保留维度，null 只用于单个不分组汇总”；这不是自动修写模型计划，也不限制合法总体 SQ。v9 的通过结果另列于下，以上 v8 结果不冒充当前版本通过。

v6 单项诊断中，SQL 参数已为空，但生成了重复 `hypothesis_key`，Pydantic 以 `provider_invalid` 拒绝；原始本地模型响应另外留档。v7 将提示拆为各阶段专属的简短契约，明确唯一假设标识、避免重复查询和提前宣称原因。v7 完整 smoke 仍是 4/5：模型生成无占位符 SQL，却附带 `parameters={"min_group_n":20}`，继续被 `sql_parameters` 拒绝。

v8 从模型工具 schema 中移除 `parameters`，SQL 只用字面量；返回端拒绝任何显式参数字典，不是静默剥离错误字段。底层 ToolRequest/Verifier 仍保留命名绑定及精确键集合校验。新增 6 项假设/反思回归覆盖空字典、非空字典和 null 参数拒绝；89 项 provider/managed/SQ/oracle 专项通过（5.11 秒），真实 Qwen 单项假设检查 1/1 通过（28.846 秒）。完整五项和全流程另行复验，不能用单项替代。1–3 个假设的模型 schema、各类程序闸门以及所有调用/时间预算均未放宽。

第四次完整运行（开发 v9）通过：395.073 秒（含图外 BGE 入库/校准），core 与明确授权的材料批次追问均 completed；累计 5 次生成、4 次工具、3279 tokens，未扩大原预算。计划维度、6200 条观察的 2 产线/8 批次统计与新旧 Evidence 身份均通过。但随后审查补充了条件聚合越维度、总体 SQ 缺指标取证、原始 SQL 投影与统计混展示、文档适配器及异常响应/字面量边界修复，**该开发通过不等于最终 v10 验收**。

v10 已回归这些修复：SQL 指标 SQ 的表/聚合/投影维度检查、metadata 不替代 SQL、正式报告只含程序统计（原始 SQL 投影仍在 Evidence 审计制品中）；模型返回 envelope 显式校验，真实 SQL 字符串允许注释样式字符但仍拒绝实际注释。对应的 4 个旧反思/恢复测试曾把 metadata 当成指标修复，因此被新闸门拒绝；测试改为真正的 SQL 修复后，80 项 SQ/图/恢复专项通过（11.81 秒），没有放宽新闸门。

文档适配器修复保留精确的已签发引用，将工厂/源版本等外层字段与 RetrievalHit 分开，避免缺失工厂导致拒绝以及富化字段导致严格报告校验失败；来源明确为历史文档工厂/ACL 范围，不伪称合成业务统计。14 项新增回归覆盖真实入库→检索→Verifier→报告及拒绝边界。初次 2 项失败源于测试误写 ACL 撤销/删除的错误码，实际均已正确拒绝；按现有不泄露策略精确断言 `document_unavailable` 后，全量 435 项通过。保留开发及失败记录，不把运行中的旧代码进程视为已包含后续修复。

第五次完整运行（当前 v10）在全新目录通过，369.130 秒（含图外入库/校准）。准备阶段 2 次生成/1 次工具/818 tokens；核心完成时 3/2/1412；授权材料追问完成时累计 5/4/3419。core 仅授权 `core-1`，追问授权 `core-1` 与 `extended-2`，各阶段 `last_error=null`。独立 oracle 核对 6200 条观察、2 条产线、8 个材料批次的统计，以及新 Evidence 身份、正式报告无原始 SQL 投影。原预算 20/24/24000/300 未放宽，图外时间另计；没有云或 SyntheticProvider 回退。

本次 smoke 与全流程使用相同指纹 `local-bge-cpu-cb97b510d6c63df0dd60f75940b482f9b4b43cf8762e2a605f99701bb9676190`。重复校准仍是相同极小 fixture，不是新增独立 benchmark；模型提出的假设仍不等于统计或因果结论。

## 复现

在 Python 3.12 项目环境和已准备的本地运行配置下：

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
node scripts/browser_smoke.mjs

.venv/bin/python scripts/real_model_smoke.py \
  --config "$QA_RUNTIME_CONFIG" --output runtime/local-smoke-result.json
.venv/bin/python scripts/real_model_analysis.py \
  --config "$QA_RUNTIME_CONFIG" --root runtime/local-acceptance
```

smoke 输出必须是新文件且父目录已存在；完整验收 root 必须不存在。不要覆盖或删除失败记录、扩大预算只为让测试通过，或切成 SyntheticProvider 后称为真实模型验收。

## 发行物与安全扫描

```bash
uv build --sdist --wheel --out-dir dist --python .venv/bin/python \
  --offline --no-python-downloads --no-build-isolation --no-config
.venv/bin/python scripts/check_release.py \
  --archive dist/quality_analysis_agent-0.1.0.tar.gz \
  --archive dist/quality_analysis_agent-0.1.0-py3-none-any.whl
.venv/bin/python scripts/check_wheel.py \
  --wheel dist/quality_analysis_agent-0.1.0-py3-none-any.whl
```

首次无隔离构建缺少 Hatchling，失败后从已有缓存显式安装 Hatchling 1.33.0 等构建依赖，随后离线构建成功；未修改依赖声明来掩盖失败。首次新 venv 的 requirements 索引式离线解析缺 NumPy 缓存元数据而失败，改为直接使用原锁文件的 `uv sync --frozen --no-dev --no-install-project --offline` 成功；再通过标准安装器安装实际 wheel。两次结果分别保留，未联网补包。后一次还运行 `uv pip check`、安装后的 quality 入口、源码外资源/API 和 synthetic demo。

`check_wheel.py` 自身是 `python -I -S` + 当前已安装依赖目录的源码隔离 probe，不是 pip/uv 安装；上述全新 venv 是另行实际执行的标准安装验证。不得混淆二者。未验证全新操作系统或在线重新解析依赖。

`check_release.py` 仅扫描 Git 的 cached + 非忽略 untracked 候选，使用严格路径 allowlist。被强行跟踪的忽略文件、符号链接、未知路径、超大文件直接拒绝，不读取其内容；模型、数据库、运行配置、环境文件不属于发布载荷。常见凭据与私有 home 链接检测仅给规则名/相对路径，不输出匹配内容。三个假 URL 的例外绑定到既有测试文件、规则、精确字面值，不允许任意跳过。

默认不读取 dist；只有显式指定归档才作受限条目/大小/路径/内容检查。扫描不是完整秘密检测、许可证法律审查或供应链证明。首次扫描发现浏览器脚本注释的本机路径，改为通用 node 命令后通过。最终文件必须再检查，私有仓库也不得包含敏感材料。
