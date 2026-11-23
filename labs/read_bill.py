import pandas as pd
from pathlib import Path

PATH = Path("/Users/a1234/Downloads/微信支付账单流水文件(20261022-20261122)_20261122195801.xlsx")
HEADER_ROW = 17

df = pd.read_excel(PATH, sheet_name=0, header=HEADER_ROW)

# ① 列名映射
df = df.rename(columns={
    "交易时间": "occurred_at",
    "交易对方": "merchant",
    "金额(元)": "amount",
    "收/支":     "direction",
    "交易单号": "transaction_id",
})

# ② 符号：支出取负、收入保持正、'/' 保持正（后面归 uncounted）
def to_signed(row):
    v = float(row["amount"])
    if row["direction"] == "支出":
        return -v
    return v

df["amount"] = df.apply(to_signed, axis=1)

print(df[["occurred_at", "merchant", "amount", "direction", "transaction_id"]].head())

inc_n = (df.direction == "收入").sum();  inc_s = df.loc[df.direction=="收入", "amount"].sum()
exp_n = (df.direction == "支出").sum();  exp_s = df.loc[df.direction=="支出", "amount"].sum()
mid_n = (df.direction == "/").sum();     mid_s = df.loc[df.direction=="/", "amount"].sum()

print(f"\n收入   {inc_n:>3} 笔  {inc_s:>10.2f}   期望  7 笔  6152.22")
print(f"支出   {exp_n:>3} 笔  {exp_s:>10.2f}   期望 117 笔 -3709.54")
print(f"中性   {mid_n:>3} 笔  {mid_s:>10.2f}   期望  5 笔  9865.50")
print(f"合计   {inc_n+exp_n+mid_n:>3} 笔                 期望 129 笔")