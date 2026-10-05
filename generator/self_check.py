# -*- coding: utf-8 -*-
"""数据合理性自检脚本 v2：验证数据真实性（波动幅度、异常隐蔽性、物理一致性）"""
import sqlite3
import numpy as np
import pandas as pd

conn = sqlite3.connect(r"E:\Users\海之子\project\energy-carbon-manager\energy.db")
df = pd.read_sql("SELECT * FROM hourly_records", conn)
bd = pd.read_sql("SELECT * FROM buildings", conn)

print("=== 各楼栋90天用电 ===")
for _, b in bd.iterrows():
    s = df[df.building == b["id"]]
    total = s.load_kw.sum()
    annual = total * 365 / 90 / 1.25  # 夏季系数约1.25
    per_m2 = annual / b["area_m2"]
    print("{}: 90天 {:.0f} kWh, 折年化 {:.0f} kWh, 单耗 {:.1f} kWh/m2·a (参考: {})".format(
        b["name"], total, annual, per_m2, b["annual_kwh_per_m2"]))

print()
print("=== 负荷波动幅度（排除异常后，衡量正常数据真实性）===")
for _, b in bd.iterrows():
    s = df[(df.building == b["id"]) & (df.is_anomaly == 0)]
    # 逐日同时段（同小时）负荷的相对波动：取每日该小时值 / 中位数 - 1
    g = s.copy()
    med = g.groupby("hour")["load_kw"].transform("median")
    rel = ((g["load_kw"] - med) / med).abs()
    print("{}: 同时段相对波动 均值 {:.1%} / 95分位 {:.1%}（正常负荷波动约5-15%）".format(
        b["name"], rel.mean(), rel.quantile(0.95)))

print()
print("=== 光伏 ===")
pv_total = df.pv_kw.sum()
print("90天园区光伏发电 {:.0f} kWh, 折年化 {:.0f} kWh".format(pv_total, pv_total * 365 / 90))
print("园区总装机 800 kW, 90天等效利用小时 {:.0f} h, 折年化 {:.0f} h (山东参考1200-1400h)".format(
    pv_total / 800, pv_total * 365 / 90 / 800))
# 天气持续性检查：逐日光伏总量自相关
daily_pv = df.groupby(df.ts.str[:10])["pv_kw"].sum()
ac = daily_pv.autocorr(lag=1)
print("逐日光伏总量一阶自相关: {:.2f}（>0.3 说明天气有持续性，真实）".format(ac))

print()
print("=== 异常检查（三类）===")
anom = df[df.is_anomaly == 1]
print("异常总数 {} 条 / 总记录 {} = {:.2%}（真实园区异常比例通常1-5%）".format(
    len(anom), len(df), len(anom) / len(df)))
print(anom.groupby(["building", "anomaly_desc"]).size().to_string())
# 异常幅度统计（科学口径）：
# - 渐变类（A/C）：基准 = 异常开始前7天的同楼栋×同小时×同工作日属性正常中位数（同季节对比）
# - 间歇类（B）：基准 = 所有正常周末同小时中位数（注明受季节影响）
anom = df[df.is_anomaly == 1]
GRADIENT = {"A栋空调系统COP下降（设备效率渐变退化）": 65,
            "B栋深夜公共照明/设备老化（基荷缓慢漂移）": 70}
for desc, g in anom.groupby("anomaly_desc"):
    b = g.building.iloc[0]
    if desc in GRADIENT:
        start = GRADIENT[desc]
        base_df = df[(df.building == b) & (df.day_idx >= start - 7) & (df.day_idx < start)]
        med = base_df.groupby(["hour", "is_workday"])["load_kw"].median()
        g2 = g.copy()
        g2["base"] = [med.get((h, w), np.nan) for h, w in zip(g2["hour"], g2["is_workday"])]
    else:
        # 间歇类：正常周末（is_workday=0 且非异常）同小时中位数
        base_df = df[(df.building == b) & (df.is_workday == 0) & (df.is_anomaly == 0)]
        med = base_df.groupby("hour")["load_kw"].median()
        g2 = g.copy()
        g2["base"] = g2["hour"].map(med)
    g2["lift"] = (g2["load_kw"] - g2["base"]) / g2["base"]
    print("{}: 相对同期正常中位数 抬升中位 {:.1%} / 最大 {:.1%}（真实异常通常5-20%，渐变类早期很低）".format(
        desc, g2["lift"].median(), g2["lift"].max()))

print()
print("=== 电价抽查 ===")
print(df[["hour", "price"]].drop_duplicates().sort_values("hour").to_string(index=False))
