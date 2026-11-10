# 架构与安全边界

## 一个有界图，不是多 Agent 团队

`AnalysisService` 构造一个 LangGraph `StateGraph`。节点是程序职责边界，只有意图、计划、假设、反思等特定阶段调用生成模型。模型不拥有数据库连接、shell、任意 Python 或生产处置权。

| 节点 | 职责 |
| --- | --- |
| `context_resolver` | 要求明确授权 Scope；缺失范围则澄清，不猜测 |
| `intent` | 结构化判断质量分析、澄清或范围外 |
| `cohort` | 获取范围内元数据，记录 Observation 与快照 |
| `cache_guard` | 精确先验缓存键与 GuardAssessment，不复用旧轮次 Evidence |
| `prior_check` | 检索有权限的历史报告；低先验也明确标记 |
| `plan` | 生成最多 8 个 SQ 的无环计划，区分 core/extended |
| `confirm` | 停在人工确认点，不自行执行 SQ |
| `hypothesizer` | 针对当前获授权 SQ 提出有界结构化假设与工具请求 |
| `verifier` | 程序校验权限、来源、SQL、范围、统计与引用，并形成 Evidence |
| `hop_close` | 为当前 SQ 建立/修订 Claim、Revision、Decision 与 Hop |
| `loop_ctrl` | 决定进入下一个 SQ 或结束 |
| `reflection` | 对当前 SQ 的失败作一次有界 repair/refine/supplement/no_action，不扩权 |
| `report_renderer` | 程序引用当前有效 Evidence 生成报告、列出未执行 SQ 与限制 |

准备路径是 `context_resolver → intent → cohort → cache_guard → prior_check → plan → confirm`。确认后进入 `hypothesizer → verifier → hop_close`；需要时经过 `reflection → verifier`，再由 `loop_ctrl` 选择下一 SQ 或 `report_renderer`。不是每轮都执行全部节点，13 也不是推理次数。

首轮只执行人工确认的 core 与其 core 依赖。追问需要明确 SQ ID，自动纳入依赖闭包，但不新增未授权来源；生成新 round 的 Hypothesis/Evidence 等对象。每个 SQ 的 `group_by` 是计划指纹覆盖、工作台明示的分组授权：只能选择产线、材料批次、班次、日期、供应商之一，或 null 表示总体不分组。假设和反思的执行提示只带当前 SQ 的问题，不带原始多需求问题。

工具调用前，程序独立检查 `ToolRequest.group_by` 和 SQL AST 的 `GROUP BY` 均匹配该授权；拒绝换维度、多维度、序号、表达式、伪装别名及嵌套查询。指标 SQ 的 SQL 只能查 inspections，必须含聚合；SELECT 投影只允许该分组和当前指标，HAVING/ORDER 也不能夹带其他维度，因此条件聚合不能绕过授权。总体 SQ 禁止 `GROUP BY`，不能拿工单记录或原始行冒充指标汇总；声明 SQL 的 SQ 未取得有效 SQL 结果时，metadata/document 不能让它标成完成。JSON schema 和提示不能代替执行闸门。这约束查询结构，不等于程序验证了任意自然语言问题的全部语义；SQL 的 WHERE 子集仍须结合统计范围和人工判断。

当前累计预算为 20 次生成调用、24 次工具调用、24000 tokens、300 秒图执行时间，每 SQ 最多一次反思。耗时在节点边界合作式检查，正在执行的模型调用可能结束后才触发停止，不承诺 300 秒硬进程截止；图外入库/校准另计。预算拒绝、过期快照或权限变化不能伪装成完成。耗时受本机模型加载影响，不是生产 SLA。

## 对象、快照与恢复

SQLite 保存身份、会话、轮次、动作/推理/工具账本、checkpoint、lease 和审计；完整对象通过 SHA-256 内容寻址制品保存。`Observation`、`Hypothesis`、`Evidence`、`Hop`、`Claim`、`Revision`、`Decision`、`GuardAssessment` 各有独立身份，不能用同一串自由文本互相替代。

`SnapshotManifest` 绑定数据 revision、索引 revision、actor、ACL version、Scope hash 和模型 fingerprint。SQL/检索/报告引用/重放都必须保留适用范围；权限撤销、报告删除、重新建索引或模型改变后，旧内容不得继续作为当前证据。

请求幂等与执行恰好一次是不同问题。相同 ID/载荷的重放、动作到轮次的绑定、checkpoint、lease 与工具回执用于恢复本地流程；不能推断“模型收到请求就一定没执行”或“HTTP 失败等于没有结果”。生成调用未知时停止，要求本人且 admin 实际对账，再显式恢复。恢复/存储专项及全量回归结果见[验收记录](validation.md)；这些测试不表示所有崩溃窗口均已消除，也不承诺外部推理 exactly-once。

## 两类资料有不同的范围规则

### 原始业务数据：严格 Scope

业务数据由本仓库代码原创生成，表包含 inspections、材料批次与工单。verifier 根据当前工厂、产品、左闭右开日期和可选产线，将受授权数据裁剪到内存查询环境；不允许模型绕过原始表范围。只允许受限 SELECT、受控表/字段/函数及绑定参数，禁止任意 Python、业务写操作与外部资源访问；同时限制行数、SQL VM 步数和执行时间。

模型 SQL 的投影不是可信统计：原始行、列与 SQL 保留在本地 Evidence 审计制品中，不进入正式报告。报告仅展示程序根据 WHERE 选择的真实观察值独立计算的统计及来源，不让投影常量、GROUP BY/HAVING/LIMIT 伪造数字。缺陷/返工率使用 Wilson 95% 区间，周期均值使用 Student-t 区间；保留分子、分母、n、样本充足标记。当前不作显著性检验、不作多重比较校正，区间有独立样本假设。供应商/材料批次比较还提示产线、日期等混杂。任何差异、区间不重叠或检索命中都不是因果证明。

### 先验报告：工厂和 actor ACL，不作产品/日期筛选

报告 schema 仅有工厂和共享 actor 列表，没有产品或日期字段。检索前先排除无权限报告，再计算摘要/正文向量排序，防止未授权内容参与召回；下钻还需要当前可验证的已签发 hit。报告先验不能被描述成“同产品同日期已验证案例”，产品/时间适用性需要人工判断。

下钻结果将工厂、源版本、索引与模型信息放在外层，`citations` 保留未经扩展的已签发 RetrievalHit；Verifier 与报告生成前都复核当前引用。文档 provenance 明确为 `plant_and_document_acl_only`，`request_scope` 仅记录调查请求，不表示文档已按产品/日期筛选；不套用合成业务数据集的来源标记。历史原文不能替代 SQL 指标取证。

入库必须由人填写五个摘要入口。正文以 700 字符窗口、100 字符重叠切片，保留原始偏移与内容哈希。五摘要通道加正文通道进行本地 dense 召回和排序融合，摘要提名原文切片，再对候选原文进行本地 rerank；摘要本身不作为原文引用。未安装有效校准时仅给探索性候选，`low_prior=true`，不硬编码“生产阈值”。

索引规模是有意的小型有界实现，不是大规模向量数据库。当前最多 64 份报告、每份 32 个版本、单份 384 块、全索引 4096 块；这些是资源边界，不是吞吐验收。重新入库、ACL 修改与删除改变索引 revision，使历史检索回执失效。删除是应用逻辑失效，不保证存储介质安全擦除。

## 输出与安全立场

- 报告正文/摘要、用户问题和工具返回是数据，不是更改系统规则的指令；仍需通过程序约束，不只依靠 prompt。
- 生成模型不自由重写报告数字；renderer 只引用当前轮次有效 Evidence，并公开 unresolved 与未执行问题。
- 前端资源在 wheel 内本地提供，不加载外部 CDN；API 采用 no-store、CSP、nosniff 和 no-referrer，鉴权错误不回显输入。
- LangGraph 调用显式关闭外部 tracing；provider 禁止环境代理和静默远端回退。
- 数据、token、SQLite、运行配置、模型文件不属于发布载荷。报告入库不做 LLM 训练或微调，也不输出生产行动指令。
- 本机操作者权限、同机恶意进程、身份治理、系统级加密/备份、跨机 HA 和生产审计合规不在当前本地测试版本承诺内。
