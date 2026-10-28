# 使用指南：本机、合成数据、显式授权

## 1. 用户、数据目录与模式

以下示例在仓库根目录执行，示例业务范围为 `PLANT-A` / `WIDGET-A`，`2025-01-01` 至 `2025-02-01`（右端不包含）。不要使用生产数据。`--root` 优先于 `QA_ROOT`；未指定时为 `.quality-data`。建议显式放在忽略的 `runtime/` 内，避免把 SQLite、内容寻址制品和报告带入发布。

`user`、`seed`、`serve` 都可以在子命令前后接受 `--root` 和 `--provider`。`user` 只操作本机 Store，不需要模型；其他命令使用指定 provider。`seed` 需要获授权的 admin，且拒绝覆盖已有业务数据、报告或会话。`demo` 要求空/不存在目录，并且必须显式选 `local` 或 `synthetic`；它创建演示账号但不输出原始令牌。

```bash
.venv/bin/quality --root runtime/workbench user demo-admin --role admin --plant PLANT-A
.venv/bin/quality --root runtime/workbench user demo-operator --role operator --plant PLANT-A
.venv/bin/quality --root runtime/workbench user demo-analyst --role analyst --plant PLANT-A
.venv/bin/quality --root runtime/workbench --provider synthetic seed
.venv/bin/quality --root runtime/workbench --provider synthetic serve
```

CLI 认证优先读取 `QA_TOKEN`；否则在交互终端无回显询问。适用子命令还支持 `--token-stdin`，由调用方安全管道输入一行令牌。非交互、无令牌时失败，不猜测身份。不要把真令牌写进 shell 命令、版本控制或文档。浏览器页面在 `http://127.0.0.1:8000`，默认无远程绑定、无自动登录。

| 角色 | 当前授权内允许的行为 |
| --- | --- |
| `operator` | 发起/确认/追问/恢复本人分析、看本人会话与审计、检索和下钻可见报告 |
| `analyst` | operator 行为，加报告入库/更新/上传；不能借更新已有报告变更 ACL 或迁移工厂 |
| `admin` | analyst 行为，加 ACL 修改、报告删除、检索校准、示例 seed；未知调用对账还要求会话归本人所有 |

admin 不是“查看所有人会话”的超级读权限。报告 `shared_with=[]` 表示对该工厂所有获授权 actor 可见；非空列表表示仅列出的 actor 可见，仍需拥有该工厂授权。无“角色绕过报告 ACL”的隐含规则。本地开户依赖操作者文件系统权限，不能当作生产身份治理。

## 2. 计划、确认、追问

```bash
.venv/bin/quality --root runtime/workbench --provider synthetic analyze \
  '比较各产线缺陷率，列出后续分层取证计划，不认定因果' \
  --request-id original-analysis-1 \
  --plant PLANT-A --product WIDGET-A \
  --start 2025-01-01 --end 2025-02-01 --metric defect_rate
```

正常准备完成会返回 `awaiting_confirmation`、`session_id`、`plan` 和 `plan_fingerprint`。读取实际返回值，再输入确认；不要把下面变量名当作真实返回值。`--sq` 可重复传入。

```bash
.venv/bin/quality --root runtime/workbench --provider synthetic confirm "$SESSION_ID" \
  --action-id original-confirm-1 --sq core-rate --plan-fingerprint "$PLAN_FINGERPRINT"

.venv/bin/quality --root runtime/workbench --provider synthetic followup "$SESSION_ID" \
  '授权按材料批次分层，并重新取证所需依赖' \
  --request-id original-followup-1 --sq lot-strata
```

这里的 `core-rate`、`lot-strata` 只适用于 `SyntheticProvider` 的固定计划。真实模型产生的 SQ ID 必须从其计划读取，不能硬编码。确认前还要检查标题、来源和页面显示的 **SQL 分组授权**：`line_id` 是产线，`material_lot` 是材料批次，`shift` 是班次，`day` 是日期，`supplier_id` 是供应商，null 是总体不分组。一个 SQ 不得同时授权多个分组维度；其他维度要列为独立的后续 SQ。

确认检查包含分组授权的计划指纹和依赖闭包；首轮不得含 `extended`。追问仅能从已结束轮次开始，沿原计划显式选 SQ 和依赖，继承原授权范围，不会自动扩大工厂、日期或所选 SQ 的分组维度。分组不匹配的 SQL 会在执行前拒绝，Reflection 也不能绕过。

Scope 参数约束：

- 指标 `defect_rate`、`rework_rate`、`cycle_time_mean`。
- 日期区间 `[start_date, end_date)`，长度 1–366 天。
- `--line` 可重复，至多 20 个且不得重复；留空表示该 Scope 下所有产线。
- `--min-group-n` 默认 20，允许 5–500；它不是因果或显著性的保证。
- 标识符为 ASCII 字母/数字起始，其后可含字母、数字、`_`、`.`、`-`，总长度不超过 100。

同一 actor 的同一请求/动作 ID 用来辨认重放；不要用同一 ID 提交不同载荷。数据、索引、权限或模型改变导致快照失效时，应新建分析，而非强行复用旧确认或证据。

## 3. 恢复与人工对账

`needs_clarification` 需要补全范围并以新请求 ID 发起；`out_of_scope` 不执行调查。`completed` 只是程序完成授权观察，`limited` 表示存在未解决部分，都不是物理根因验收。`blocked` 不保证可恢复；仅可恢复的 provider 失败才接受显式 retry。

```bash
.venv/bin/quality --root runtime/workbench --provider local calls "$SESSION_ID"
.venv/bin/quality --root runtime/workbench --provider local retry "$SESSION_ID" \
  --action-id manual-retry-1
```

出现 `reconciliation_required`、`started` 或 `unknown` 调用时，禁止自动重试。需要**本人且 admin** 实际核查；普通 operator 不能以另一个 admin 身份越权对账本人会话。

```bash
# 只有确实核查无结果，才可声明 --no-result；不会立即自动恢复。
.venv/bin/quality --root runtime/workbench --provider local reconcile \
  "$SESSION_ID" "$CALL_ID" --no-result --note '已核查本地调用记录，确认没有可恢复结果'
```

另一选择是 `--receipt-stdin`，从 stdin 读取真实 JSON 回执 `{"value": {...}, "tokens": 正整数}`，此时认证使用 `QA_TOKEN`（或交互提示），不要把凭据混入回执。回执必须符合原阶段 schema；不得编造回执以跳过失败。CLI `reconcile` 不支持 `--token-stdin`。对账完成后仍需另行执行 `retry`。

## 4. HTTP API

服务仅监听回环地址。`/` 与固定 `/static/app.js`、`/static/style.css` 提供前端。`GET /api/health` 无需登录，仅返回进程状态；`inference_checked` 固定为 `false`。没有 Swagger/OpenAPI 页面。

其他 `/api/` 路由使用 `Authorization: Bearer <令牌>` 请求头；query string、正文中的 token 不能代替该请求头。除无正文操作外，正文为 UTF-8 JSON 对象、`Content-Type: application/json`。实际 ASGI 请求流限制为 1 MiB，拒绝 NaN/Infinity、未知字段和超界输入。以下仅列结构，不包含真实凭据。

| 方法、路径 | 正文/用途 |
| --- | --- |
| `GET /api/me` | 当前 actor、provider 和可见的示例 Scope |
| `GET /api/sessions` | 本人会话列表 |
| `POST /api/sessions` | `request_id, question, scope`；省略 scope 会进入澄清而非自行推断 |
| `GET /api/sessions/{session_id}` | 本人会话与有效报告 |
| `POST /api/sessions/{session_id}/confirm` | `action_id, selected_sq_ids, plan_fingerprint` |
| `POST /api/sessions/{session_id}/followup` | `request_id, question, selected_sq_ids` |
| `POST /api/sessions/{session_id}/retry` | `action_id` |
| `GET /api/sessions/{session_id}/calls` | 调用 ID/类型/状态，不返回提示词 |
| `POST /api/sessions/{session_id}/calls/{call_id}/resolve` | admin 且本人；`note` 10–1000 字，二选一：`result` 真实回执或 `authorize_retry_without_result: true` |
| `GET /api/sessions/{session_id}/audit` | 本人会话审计 |
| `POST /api/documents/ingest` | analyst/admin；下述 `DocumentInput` |
| `POST /api/documents/upload` | analyst/admin；元数据、五摘要、`filename, content_base64`；不是 multipart 表单 |
| `POST /api/documents/search` | `query, scope`；仅按工厂+actor ACL 限制报告 |
| `GET /api/documents/{document_id}/chunks/{chunk_id}` | 下钻已签发且仍有效的原文引用 |
| `PATCH /api/documents/{document_id}/acl` | admin；`shared_with` 列表 |
| `DELETE /api/documents/{document_id}` | admin；使文档/索引引用失效，不承诺磁盘安全擦除 |
| `POST /api/documents/calibrate` | admin；校准请求见下文 |

API 创建分析的示例 JSON：

```json
{
  "request_id": "original-api-1",
  "question": "比较各产线缺陷率，不作因果结论",
  "scope": {
    "plant_id": "PLANT-A",
    "product_id": "WIDGET-A",
    "start_date": "2025-01-01",
    "end_date": "2025-02-01",
    "metric_id": "defect_rate",
    "line_ids": [],
    "min_group_n": 20
  }
}
```

## 5. 报告入库与校准

`DocumentInput` 必须包括 `document_id, title, plant_id, summaries, text`，可带 `shared_with`。五摘要键恰好是 `phenomenon`（现象）、`context`（背景）、`hypothesis`（假设）、`evidence`（证据）、`action`（措施）；每段 2–1000 字符，人工填写，不能把假设伪装成已核验证据。正文 40–200000 字符。当前 schema **没有产品、报告日期字段**；即使搜索请求带完整 Scope，历史报告也不做产品/日期筛选，操作者必须审核适用性。

上传支持 UTF-8 `.txt`、`.md` 和有文本层的 `.pdf`；不做 OCR、不破解加密 PDF、不读取附件中的外部链接。解析器本身限制 8 MiB / 200 页 / 200000 字符，**HTTP JSON 的 1 MiB 总限制更小，base64 还增加体积**，不能声称 API 可上传 8 MiB。PDF 引用偏移基于抽取后按页拼接的文本，不是 PDF 二进制字节偏移。

`POST /api/documents/calibrate` 例如：

```json
{"plant_id":"PLANT-A","use_original_cases":true,"install":false}
```

这是 API 默认的英文原创合成标签集；并不自动安装阈值。`install:true` 才会显式安装，仍仅对当前 provider、actor/ACL 和索引快照适用。也可提供 `cases` 列表，不能与 `use_original_cases:true` 同时选择。每个 case 包含 `case_id, split, query, scope, relevant_document_ids`；split 为 `fit` 或 `eval`，每个 split 均需正例与无答案样例。

Python `original_calibration_cases(plant_id, language="zh")` 提供相同文档分割的中文版本。每种语言各 12 个原创查询，fit/eval 的正标签文档不交叉；英语和中文是对应 fixture，不是两套独立生产证据。真实全流程脚本显式选择中文版；API 默认没有 `language` 参数。更多评测含义见[验收记录](validation.md)。
