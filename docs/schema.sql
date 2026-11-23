-- 项目 B · 钱管家 数据模型
-- 金额一律 NUMERIC、带符号（支出为负）：见 docs/caliber.md 第 1 条

CREATE TABLE IF NOT EXISTS bills (
    id           bigserial PRIMARY KEY,             -- 代理键：给 transactions 做外键
    platform     text NOT NULL,                     -- 'wechat' / 'alipay'；参与唯一键，必须 NOT NULL
    file_name    text NOT NULL,                     -- 源文件名，仅追溯来源
    period       date NOT NULL,                     -- 文件起始月（当月 1 号）；按月统计看 occurred_at，不查此列
    total_count  integer NOT NULL DEFAULT 0,        -- 库中明细总数（非本次插入数）
    total_amount numeric(14,2) NOT NULL DEFAULT 0,  -- 明细金额合计
    imported_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, period)                       -- 同一平台同一月只允许一个账单文件
);

CREATE TABLE IF NOT EXISTS transactions (
    id             bigserial PRIMARY KEY,
    bill_id        bigint NOT NULL REFERENCES bills(id) ON DELETE CASCADE,  -- 删账单级联删明细，防孤儿
    transaction_id text NOT NULL,                   -- 平台流水号，不限制长度
    merchant       text NOT NULL,
    category       text,                            -- 可空：导入时未知，由 web_search / 缓存回填
    amount         numeric(12,2) NOT NULL,          -- 带符号：收入正、支出负
    occurred_at    timestamp NOT NULL,              -- 本地时间（微信声明 UTC+08:00），不做时区转换
    UNIQUE (bill_id, transaction_id)                -- 联合唯一；不同账单流水号允许碰撞
);

CREATE INDEX IF NOT EXISTS idx_tx_occurred     ON transactions (occurred_at);
CREATE INDEX IF NOT EXISTS idx_tx_cat_occurred ON transactions (category, occurred_at);  -- 等值列在前，范围列在后
CREATE INDEX IF NOT EXISTS idx_tx_merchant     ON transactions (merchant);

CREATE TABLE IF NOT EXISTS merchant_category (
    merchant    text PRIMARY KEY,                   -- 商户名即业务主键：查询永远按它
    category    text NOT NULL,
    confidence  numeric(3,2),                       -- 0.00~1.00；人工确认时为 NULL
    source      text NOT NULL,                      -- 'llm' / 'manual'
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (category IN ('餐饮','交通','购物','居住','娱乐','医疗','教育','人情','工资','理财','转账','其他'))
);
