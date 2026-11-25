# 项目 B · 工具契约

四个工具的输入输出契约。实现方（人或 AI）应能仅凭本文档写出实现，签名无需再改。

---

## import_bill

**目的**：把一个账单文件导入库，幂等；重复导入同一平台 + 月份账单属于正常场景，返回成功并标记，不报错。

### 参数

| 名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file_path | str | 是 | 账单文件绝对路径，支持 `.csv` / `.xlsx` |
| platform | str | 是 | `wechat` / `alipay`，枚举校验 |
| period | str | 是 | `YYYY-MM`，正则校验，拼接 `-01` 后转 date（当月 1 号） |

### 成功返回

```json
{"ok": true, "bill_id": 1, "duplicate": false, "inserted": 118, "skipped": 2, "total_amount": "-3421.50"}
```

重复导入同一平台 + 月份账单：

```json
{"ok": true, "bill_id": 1, "duplicate": true, "inserted": 0, "skipped": 120, "total_amount": "-3421.50"}
```

### 失败返回

`{"ok": false, "code": "...", 附加字段...}`

| code | 附加字段 |
|---|---|
| FILE_NOT_FOUND | `path` |
| ENCODING_ERROR | `tried`: ["utf-8","gbk"]（仅 `.csv`） |
| MISSING_COLUMN | `missing`: [...], `file_header_sample`: [...] |
| BAD_ROW | `line`: 37, `raw`: "...", `reason`: "金额解析失败" |
| COUNT_MISMATCH | `expected`: 120, `actual`: 118 |

### 平台字段映射（MISSING_COLUMN 校验依据）

| platform | 时间列 | 金额列 | 商户列 | 流水号列 |
|---|---|---|---|---|
| wechat | 交易时间 | 金额(元) | 交易对方 | 交易单号 |
| alipay | 交易时间 | 金额 | 交易对方 | 交易号 |

### 约束

1. `file_rows` 口径：表头之后的**非空数据行数**；表头定位规则：找到包含该平台【时间列、金额列】的行；前置说明行、表头行、末尾空行不计入
2. 列名匹配规则：列名先 `strip()`，并做全角/半角括号归一化后再比对；`MISSING_COLUMN` 时附带返回文件原始表头样例，便于核对
3. 流水冲突判定：使用 `ON CONFLICT (bill_id, transaction_id) DO NOTHING`，`inserted = cur.rowcount`，`skipped = len(rows) - inserted`；**禁止解析阶段预查询判断冲突（check-then-act）**
4. `COUNT_MISMATCH` 校验：导入后查询数据库 `count(*)` 作为 `actual`，与 `file_rows` 对比；**不能用内存计算值自校验**；不匹配则回滚
5. 单行解析失败（`BAD_ROW`）：**整单回滚**。理由：账单是全量快照，缺行的报表具有误导性，风险高于无报表
6. 金额返回为字符串，保留两位小数，规避浮点精度丢失
7. 整体包裹在单一事务内；所有故障类失败路径必须回滚，不残留半截账单/明细/汇总
8. `UNIQUE(platform, period)` 冲突时不返回错误码：回查已有 `bill_id`，继续插入明细（全部冲突），返回 `duplicate: true`
9. `.xlsx` 读取时表头行不可硬编码，须按特征词定位（含时间列名且含金额列名的那一行）
10. `.xlsx` 中金额为数值型，入库时交给 `numeric(12,2)` 补零，不在 Python 侧做字符串格式化

---

## query_db

**目的**：按月（可叠加类别/商户）聚合账单，返回可核对的数字；区分参与收支统计与不计收支的交易（转账、理财），消除口径歧义。

### 参数

| 名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| month | str | 是 | `YYYY-MM`；**不支持查全部**，避免全表扫描 |
| category | str | 否 | 精确匹配；数据库内 NULL 统一归为「未分类」 |
| merchant | str | 否 | 精确匹配 |
| group_by | str | 否 | `category` / `merchant` / null |

### 成功返回

```json
{
  "ok": true,
  "month": "2026-11",
  "count": 40,
  "count_all": 42,
  "income": "12000.00",
  "expense": "-15421.50",
  "net": "-3421.50",
  "uncounted": {"count": 2, "sum": "-500.00"},
  "rows": [{"key": "餐饮", "count": 18, "income": "0.00", "expense": "-890.00", "net": "-890.00"}]
}
```

当月无数据（正常状态，不是错误）：

```json
{"ok": true, "month": "2026-10", "count": 0, "count_all": 0,
 "income": "0.00", "expense": "0.00", "net": "0.00",
 "uncounted": {"count": 0, "sum": "0.00"}, "rows": []}
```

### 失败返回

| code | 附加字段 |
|---|---|
| BAD_MONTH | `month`: "2026-13" |
| BAD_GROUP_BY | `got`: "date" |

### 约束

1. 金额分 `income` / `expense` / `net` 三项返回；**`expense` 保持负数**，与 `transactions.amount` 带符号口径统一
2. 分类处理：`COALESCE(category, '未分类')`
3. 时间查询使用半开区间：`occurred_at >= 'YYYY-MM-01' AND occurred_at < 下一月第一天`；不使用 `<= 月末`（规避 timestamp 小数秒丢数据）
4. 指定 `group_by` 时，`rows` 按 `abs(net)` 降序；`rows` 内每组的 `count` 同样只统计计入收支的交易
5. 顶层 `count`/`income`/`expense`/`net` **恒为过滤后、计入收支的合计，不受 `group_by` 影响**；`rows` 各组相加应等于顶层（自检条件）
6. 所有金额、条数由 SQL 聚合计算；模型不得自行加减或估算
7. 过滤条件：`month` 必选；`category`、`merchant` 为附加等值过滤，可同时传入
8. 金额以字符串返回，保留两位小数
9. 当月无数据属正常场景，返回 `ok:true`、金额置 0、`rows` 为空，不返回错误码
10. 顶层聚合字段仅统计**计入收支**的行（`category NOT IN ('转账','理财')`）；被排除的交易数与金额合计放在 `uncounted`
11. `count_all`：本次过滤范围内全部交易笔数（含转账、理财）
12. `group_by` 的 `rows` 只包含计入收支的类别/商户；转账、理财不出现在 `rows`，只在 `uncounted` 体现（保证 `sum(rows.net) == 顶层 net`）
13. rows最多返回 40 行（按 abs(net)降序取前 40）；超出时 rows_truncated: true。模型据此知道「还有更多」，需要时可用 merchant参数缩小范围。

---

## classify_merchant

**目的**：根据商户名判断账单分类，输出候选 + 置信度。**不联网搜索**——常见商户由规则/LLM 判断，结果写入 `merchant_category` 缓存；所有候选必须落在固定枚举内。

### 参数

| 名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| merchant | str | 是 | 交易对方 / 商户名称 |
| amount | str | 否 | 金额字符串（带符号），辅助区分小额购物 / 大额转账 |
| category_hint | str | 否 | 备注、商品标题等线索 |

### 固定分类枚举（仅允许这 12 项）

`餐饮 / 交通 / 购物 / 居住 / 娱乐 / 医疗 / 教育 / 人情 / 工资 / 理财 / 转账 / 其他`

### 成功返回

```json
{"ok": true, "cached": true,
 "candidates": [{"category": "购物", "confidence": 0.92},
                {"category": "转账", "confidence": 0.05}]}
```

上层 Agent 自行决定选用哪一条；置信度仅辅助决策，不在此写死阈值。

### 失败返回

| code | 附加字段 | 说明 |
|---|---|---|
| NOT_FOUND | `merchant` | 无有效信息，无法给出候选 |
| AMBIGUOUS | `merchant`, `raw_hint` | 信息不足、歧义极强 |
| RATE_LIMITED | `retry_after_sec` | 限流 / 超时；超时场景为 null |
| BAD_CATEGORY | `got`, `allowed` | 候选自检全部不在枚举内 |

### 约束

1. 所有 `category` 必须严格属于 12 枚举；非法候选丢弃；全部非法则返回 `BAD_CATEGORY`
2. 禁止自由生成新词（吃饭、Food、日用品等）
3. `amount` 仅作消歧信号；正负可辅助区分收入/支出
4. 候选列表长度 1~3
5. 本工具**只输出候选，不写数据库**；入库由上层 Agent 决定
6. 置信度范围 `0.0~1.0`，保留两位
7. 查询顺序：先查 `merchant_category` 缓存（`merchant` 为主键），命中即返回（`cached: true`）；未命中才联网，拿到合法结果后写入缓存
8. 同一商户重复查询必须走缓存，保证分类稳定
9. 单次联网搜索超时 10 秒；超时返回 `RATE_LIMITED`（`retry_after_sec: null`），禁止无限阻塞
10. 禁止编造商户信息；信息不足时优先 `AMBIGUOUS`，不强行给高置信分类

---

## write_report

**目的**：接收带占位符的报告 Markdown 模板，拉取对应月份真实账单数据替换占位符，落地成 md 文件；隔离模型文本生成与数字来源，杜绝编造金额，同时防御路径穿越。

### 参数

| 名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| month | str | 是 | `YYYY-MM`，用于拉取数据 |
| template | str | 是 | 模型输出的 Markdown 正文，内含占位符 |
| output_path | str | 是 | 目标文件名/相对路径，最终必须落在 `REPORTS_DIR` 内 |

### 占位符规范（固定）

```
{{total_count}}  {{income}}  {{expense}}  {{net}}  {{group_rows}}
```

- `{{group_rows}}` 渲染成分类分组表格（来自 `query_db` 的 `rows`）
- 不支持表达式或四则运算（**不允许 `{{income - expense}}`**）
- 数值替换后保持字符串、两位小数，与 `query_db` 口径一致

### 成功返回

```json
{"ok": true, "month": "2026-11", "file_path": "/opt/ai-partner/reports/2026-11_bill.md", "rendered": true}
```

### 失败返回

| code | 附加字段 | 说明 |
|---|---|---|
| BAD_MONTH | `month` | 月份格式非法 |
| UNKNOWN_PLACEHOLDER | `placeholder` | 出现未定义的占位符 |
| PLACEHOLDER_MISS_DATA | `placeholder` | 数据缺少对应字段 |
| WRITE_FAILED | `path`, `errno`, `msg` | 写入失败；仅返回 errno 与简短提示，不回原始异常栈 |
| PATH_DENIED | `requested`, `allowed_dir` | 目标路径超出报告目录 |
| TEMPLATE_TOO_LARGE | `bytes` | 模板超过上限 |

### 约束

1. 工具内部自动调用 `query_db(month, group_by="category")` 取数；**模型不得自行传入数字**
2. 模型只能写文字、结论、建议；**禁止直接写金额、笔数**
3. 占位符必须完整匹配；出现非法占位符直接失败，不静默丢弃
4. 替换只做文本替换，不做任何计算；数字来源唯一
5. 路径安全：固定根目录 `REPORTS_DIR`，用 `Path(output_path).resolve().is_relative_to(REPORTS_DIR.resolve())` 校验；不通过返回 `PATH_DENIED`；写入时使用 resolve 后的绝对路径
6. 模板大小上限 **100KB**，超出返回 `TEMPLATE_TOO_LARGE`
7. 文件写入默认**覆盖**
8. 模板原样保留换行、标题、列表，仅替换占位符
9. `query_db` 返回空数据时占位符填 `0.00`，正常生成报告
10. 不做 AI 总结、不做推理；职责仅：取数 → 替换 → 落盘
