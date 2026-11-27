# money-keeper · 钱管家

把自己的微信账单导入 Postgres，用自然语言问「这个月餐饮花了多少」，
拿到每一个都能用 SQL 复算的数字。

**已上线**：https://agent.tree-studio.cn/docs

## 公网地址

- 项目 A（AI 笔记问答，RAG）：https://ai.tree-studio.cn/docs
- 本项目（账单分析 Agent）：https://agent.tree-studio.cn/docs

数据为本人真实微信账单（129 笔 / 2026-10-22 ~ 2026-11-22），
接口只返回聚合结果，不返回原始交易明细。

## 能做什么

```
$ python -m app.cli "11 月餐饮花了多少"
2026 年 11 月餐饮支出 -886.68 元，38 笔，无收入。

$ python -m app.cli "帮我出一份 11 月报告"
报告已生成：reports/2026-11_bill.md

$ python -m app.cli "这个月比上个月多花了吗"
不能直接说「多花了」，因为两个月的数据窗口差别很大：
11 月覆盖 11-01 ~ 11-22（22 天），10 月只覆盖 10-22 ~ 10-31（10 天）。
```

**第三个例子是这个项目的核心**——它拒绝了回答。

## 四个设计决定

### 1. 数字只从工具来，模型只负责叙述

`query_db` 是唯一数字来源。系统提示词明令禁止模型做加减推导
（「95 - 72 = 23」这种也不行），占比、环比工具没提供就直接说没有。

**理由**：LLM 算错的数字看起来和算对的数字长得一模一样。

### 2. 数据窗口不同就不许比（`coverage`）

账单文件是 `20261022-20261122`，所以 10 月只覆盖 10 天、11 月 22 天。
直接比金额会得出「11 月多花了」——错的是天数，不是消费。

`query_db` 因此返回：

```json
"coverage": {"start": "2026-11-01", "end": "2026-11-22",
             "days": 22, "month_days": 30, "complete": false}
```

`complete=false` 时，模型必须说明窗口不同，只能定性描述。

**这条比任何功能都重要**：工具的价值不是给出数字，是**不给错结论**。

### 3. 幂等由数据库约束保证，不交给规则判断

`UNIQUE (bill_id, transaction_id)` + `ON CONFLICT DO NOTHING`，
重复导入 `inserted=0`。`transaction_id NOT NULL` 是硬要求——
参与唯一键的列允许 NULL 时，PostgreSQL 的 `NULL != NULL` 会让约束静默失效。

商户分类缓存同理：`ON CONFLICT DO UPDATE ... WHERE source <> 'manual'`，
手工修正过的不被 LLM 覆盖。

### 4. 分不清的一律不算收支

个人姓名/昵称（含「姐」「老妈」这类亲属）一律 `category='转账'`，不计收支。
`net` 的含义是「我自己花了多少」，不是「所有进出钱」。

代价：AA 分摊、代付被排除（11 月约 600 元）。
选择理由：工具定位是**消费分析**，不是记账。

## 口径表

16 条判定规则写在 [`docs/caliber.md`](docs/caliber.md)：符号来源、退款归月、
转账/提现的内部划转判定、去重键、两位小数 ROUND_HALF_UP、校验基准分层
（解析层对账单头部，业务层对口径表，两层不得互相校验）。

## 架构

```
微信 xlsx
  → import_bill（解析 + 幂等入库 + 整单回滚）
  → merchant_category（LLM 一次批量判定 + confidence=0 占位防重判）
  → query_db（半开区间 + FILTER 聚合 + coverage）
  → agent.run（DeepSeek function calling，MAX_STEPS=6）
      ├→ write_report（占位符模板 → reports/*.md）
      └→ FastAPI /chat（同步 def，不阻塞事件循环）
```

## 快速开始

```bash
docker compose up -d
cp .env.example .env
psql "$DATABASE_URL" -f docs/schema.sql
python -m app.cli "看看 11 月账单"
```

## 已知限制

- 只有一个人用，没有多用户和鉴权
- 只支持微信账单，支付宝未支持
- 个人转账不计收支，AA 分摊会被低估
- 环比、占比工具暂不提供，模型会如实说不提供

## 设计文档

- [`docs/schema.sql`](docs/schema.sql) — 数据模型
- [`docs/tool_contracts.md`](docs/tool_contracts.md) — 工具输入输出契约
- [`docs/caliber.md`](docs/caliber.md) — 16 条口径表

## 为什么先写契约再写代码

账单数字错了比没有数字更危险。所以先把「什么算支出、退款算哪个月、
转账算不算」这些**判定句**写死，再让代码去实现——而不是写完代码再补规则。
