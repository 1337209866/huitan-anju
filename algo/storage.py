# -*- coding: utf-8 -*-
"""
慧碳安居 · 储能调度模块
--------------------------------
算法选型：线性规划（scipy.optimize.linprog）
- 目标：最小化园区日购电费用（电费 = 从电网购电量 × 分时电价）
- 决策变量：每小时储能充电量 / 放电量
- 约束：储能容量上下限、充放电功率上限、能量守恒（含效率）、购电量≥0
- 输出：最优充放电计划 + 相比"无储能"的省钱金额

【说明】园区负荷 = 各楼用电 - 光伏出力；缺额由电网购电 + 储能补充。
"""
import os
import sys
import sqlite3
import numpy as np
import pandas as pd
from scipy.optimize import linprog

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy.db")


def load_day(day_str):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT * FROM hourly_records WHERE ts LIKE ?", conn, params=(day_str + "%",))
    conn.close()
    return df


def optimize_day(day_str, cap_kwh=None, power_kw=None, eff=None, init_soc=0.2):
    cap_kwh = cap_kwh or config.STORAGE_CAPACITY_KWH
    power_kw = power_kw or config.STORAGE_POWER_KW
    eff = eff or config.STORAGE_EFF
    df = load_day(day_str)
    if df.empty:
        return None
    g = df.groupby("hour").agg(load=("load_kw", "sum"), pv=("pv_kw", "sum"),
                               price=("price", "mean")).reset_index()
    g["net_load"] = g["load"] - g["pv"]                    # 净负荷（>0 需购电）
    net = g["net_load"].values
    price = g["price"].values
    h = 24

    # 变量: [buy_0..23(24), charge_0..23(24), discharge_0..23(24), soc_0..24(25)]
    n_buy, n_ch, n_dis = h, h, h
    n_soc = h + 1
    n = n_buy + n_ch + n_dis + n_soc
    OFF_SOC = n_buy + n_ch + n_dis      # soc 块起始索引

    c = np.zeros(n)
    c[:n_buy] = price                       # 最小化购电费

    A_ub, b_ub = [], []
    # 1) 充/放电功率上限
    for t in range(h):
        row = np.zeros(n); row[n_buy + t] = 1; A_ub.append(row); b_ub.append(power_kw)
        row = np.zeros(n); row[n_buy + n_ch + t] = 1; A_ub.append(row); b_ub.append(power_kw)
    # 2) SOC 上限
    for t in range(h + 1):
        row = np.zeros(n); row[OFF_SOC + t] = 1
        A_ub.append(row); b_ub.append(cap_kwh)

    # 等式组（关键：能量平衡用等式，杜绝"凭空放电"套利）：
    # (a) 能量平衡: buy[t] + discharge[t] = net[t] + charge[t]
    #     （buy≥0 ⇒ 放电只能真实替代购电）
    # (b) SOC 递推: soc[t+1] = soc[t] + charge*eff - discharge/eff
    # (c) 初/终态 SOC 闭环（当天充放平衡，防止白嫖初始电量）
    A_eq, b_eq = [], []
    for t in range(h):
        row = np.zeros(n)
        row[t] = 1                          # +buy
        row[n_buy + n_ch + t] = 1           # +discharge
        row[n_buy + t] = -1                 # -charge
        A_eq.append(row); b_eq.append(net[t])
    for t in range(h):
        row = np.zeros(n)
        row[OFF_SOC + t] = 1               # soc[t]
        row[OFF_SOC + t + 1] = -1          # -soc[t+1]
        row[n_buy + t] = eff               # +charge*eff
        row[n_buy + n_ch + t] = -1 / eff   # -discharge/eff
        A_eq.append(row); b_eq.append(0)
    # 初态 + 终态闭环
    row = np.zeros(n); row[OFF_SOC] = 1
    A_eq.append(row); b_eq.append(init_soc * cap_kwh)
    row = np.zeros(n); row[OFF_SOC + h] = 1
    A_eq.append(row); b_eq.append(init_soc * cap_kwh)

    bounds = [(0, None)] * n_buy + [(0, power_kw)] * (n_ch + n_dis) + [(0, cap_kwh)] * n_soc
    res = linprog(c, A_ub=np.array(A_ub), b_ub=np.array(b_ub),
                  A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                  bounds=bounds, method="highs")
    if not res.success:
        return {"error": res.message}

    buy = res.x[:n_buy]
    charge = res.x[n_buy:n_buy + n_ch]
    discharge = res.x[n_buy + n_ch:n_buy + n_ch + n_dis]
    soc = res.x[n_buy + n_ch + n_dis:]

    # 无储能对照：购电量 = max(net, 0)（仅正净负荷购电，光伏多余额外消纳）
    cost_no = float(sum(max(net[t], 0) * price[t] for t in range(h)))
    cost_with = float(sum(buy[t] * price[t] for t in range(h)))
    save = cost_no - cost_with

    return {
        "day": day_str, "cost_no_storage": round(cost_no, 2),
        "cost_with_storage": round(cost_with, 2), "save": round(max(save, 0), 2),
        "buy": [round(x, 2) for x in buy], "charge": [round(x, 2) for x in charge],
        "discharge": [round(x, 2) for x in discharge],
        "soc": [round(x, 2) for x in soc],
        "net_load": [round(x, 2) for x in net],
        "price": [round(x, 4) for x in price],
    }


def run(days=7):
    """取最近 days 天，返回逐日优化结果 + 汇总"""
    conn = sqlite3.connect(DB_PATH)
    dates = pd.read_sql("SELECT DISTINCT substr(ts,1,10) AS d FROM hourly_records ORDER BY d", conn)["d"].tolist()
    conn.close()
    dates = dates[-days:]
    results = [optimize_day(d) for d in dates]
    results = [r for r in results if r and "error" not in r]
    total_save = sum(r["save"] for r in results)
    total_cost_no = sum(r["cost_no_storage"] for r in results)
    total_cost_with = sum(r["cost_with_storage"] for r in results)
    return {
        "days": results,
        "total_save": round(total_save, 2),
        "total_cost_no": round(total_cost_no, 2),
        "total_cost_with": round(total_cost_with, 2),
        "save_pct": round(total_save / total_cost_no * 100, 2) if total_cost_no else 0,
    }


if __name__ == "__main__":
    r = run(days=7)
    print("=== 储能调度（线性规划，最近7天） ===")
    print("无储能电费 ¥{:.2f} → 有储能 ¥{:.2f}".format(
        r["total_cost_no"], r["total_cost_with"]))
    print("7天节省 ¥{:.2f}（-{:.2f}%）".format(r["total_save"], r["save_pct"]))
    last = r["days"][-1]
    print("\n示例日 {} 储能计划（充+/放-，kW）:".format(last["day"]))
    for t in range(24):
        act = ""
        if last["charge"][t] > 1: act = "充电 {:.0f}kW".format(last["charge"][t])
        if last["discharge"][t] > 1: act = "放电 {:.0f}kW".format(last["discharge"][t])
        if act:
            print("  {:02d}:00 电价{:.2f} 净负荷{:.0f}kW → {}".format(
                t, last["price"][t], last["net_load"][t], act))
