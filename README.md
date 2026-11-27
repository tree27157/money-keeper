# money-keeper · 钱管家

把自己的微信账单导入 Postgres，用自然语言问「这个月餐饮花了多少」，
生成带核对口径的月度报告。

**状态**：开发中（数据层已跑通：xlsx 解析 → 幂等入库 → 整单回滚；Agent 层未接入）

**已验证**：真实微信账单 129 笔入库；同一文件重复导入 `inserted=0`；
故意破坏一行 → `BAD_ROW` 且整单回滚（`bills` / `transactions` 均未新增）；
`total_amount` 与账单头部「收入 − 支出 + 中性」三项之和一致。

## 公网地址

- 项目 A（AI 笔记问答）：https://ai.tree-studio.cn/docs
- 本项目（钱管家）：https://agent.tree-studio.cn/docs

数据均为本人真实微信账单（129 笔），接口只读聚合结果，不返回原始交易明细。

## 设计文档

- [`docs/schema.sql`](docs/schema.sql) — 数据模型（bills / transactions / merchant_category）
- [`docs/tool_contracts.md`](docs/tool_contracts.md) — 四个工具的输入输出契约
- [`docs/caliber.md`](docs/caliber.md) — 16 条口径表（符号、退款、转账、去重、精度、校验分层）

## 为什么先写契约再写代码

账单数字错了比没有数字更危险。所以先把「什么算支出、退款算哪个月、转账算不算」
这些**判定句**写死，再让代码去实现——而不是写完代码再补规则。
