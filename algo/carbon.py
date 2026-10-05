# -*- coding: utf-8 -*-
"""
慧碳安居 · 碳核算模块
--------------------------------
核算方法：排放因子法（国家温室气体核算的主流方法）
    碳排放量(tCO2e) = 用电量(kWh) × 电网平均碳足迹因子(kgCO2e/kWh) / 1000
因子来源：生态环境部《关于发布2024年电力碳足迹因子数据的公告》，
          2024年全国电力平均碳足迹因子 0.5777 kgCO2e/kWh（2023年0.6205）。
"""
import os
import sys
import sqlite3
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy.db")


def load_data():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM hourly_records", conn)
    conn.close()
    df["date"] = df["ts"].str[:10]
    return df


def carbon_of(kwh):
    """用电量(kWh) → 碳排放(tCO2e)"""
    return kwh * config.CARBON_FACTOR / 1000.0


def report():
    df = load_data()
    df["co2_t"] = df["load_kw"] * config.CARBON_FACTOR / 1000.0   # 逐时碳排（t）

    # 总览
    total_kwh = df["load_kw"].sum()
    total_co2 = df["co2_t"].sum()

    # 按楼栋
    per_building = []
    for b in df["building"].unique():
        s = df[df["building"] == b]
        per_building.append({
            "building": b, "kwh": round(s["load_kw"].sum(), 1),
            "co2_t": round(s["co2_t"].sum(), 2),
        })

    # 按日（碳排趋势）
    daily = df.groupby("date").agg(kwh=("load_kw", "sum"), co2_t=("co2_t", "sum")).reset_index()
    daily["date"] = pd.to_datetime(daily["date"])

    # 按周（周环比）；剔除不完整的尾周（天数<6 视为不足一周，避免柱状图误导）
    daily["week"] = daily["date"].dt.isocalendar().week.astype(int)
    weekly = daily.groupby("week").agg(
        kwh=("kwh", "sum"), co2_t=("co2_t", "sum"), days=("date", "nunique")
    ).reset_index()
    weekly = weekly[weekly["days"] >= 6].drop(columns="days").reset_index(drop=True)

    # 光伏减排（光伏发电量同样按碳足迹因子折算）
    pv_kwh = df["pv_kw"].sum()
    pv_avoid_co2 = carbon_of(pv_kwh)

    # 同比口径：本90天 vs 前30天均值推全年（说明口径）
    last30 = daily.tail(30)["co2_t"].sum()
    return {
        "total_kwh": round(total_kwh, 1),
        "total_co2": round(total_co2, 2),
        "pv_kwh": round(pv_kwh, 1),
        "pv_avoid_co2": round(pv_avoid_co2, 2),
        "last30_co2": round(last30, 2),
        "factor": config.CARBON_FACTOR,
        "factor_source": config.CARBON_FACTOR_SOURCE,
        "per_building": per_building,
        "daily": daily[["date", "kwh", "co2_t"]].to_dict("records"),
        "weekly": weekly.to_dict("records"),
        "n_days": int(daily.shape[0]),
    }


def esg_summary():
    """生成 ESG 报告摘要（供前端/申报书引用）"""
    r = report()
    lines = [
        "【慧碳安居 · 园区碳排月报（模拟数据，口径见说明）】",
        "核算方法：排放因子法；碳足迹因子：{:.4f} kgCO2e/kWh（{}）".format(
            r["factor"], "生态环境部2024年公告"),
        "统计周期：{} 天（2026-07-01 起）".format(r["n_days"]),
        "园区总用电：{:.0f} kWh；总碳排放：{:.1f} tCO2e".format(r["total_kwh"], r["total_co2"]),
        "光伏发电：{:.0f} kWh，等效减排 {:.1f} tCO2e".format(r["pv_kwh"], r["pv_avoid_co2"]),
        "建议：结合储能调度降低峰段购电，可进一步减少碳排。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    r = report()
    print("=== 碳核算（排放因子法） ===")
    print("园区总用电 {:.0f} kWh → 碳排放 {:.1f} tCO2e".format(r["total_kwh"], r["total_co2"]))
    print("光伏发电 {:.0f} kWh，等效减排 {:.1f} tCO2e".format(r["pv_kwh"], r["pv_avoid_co2"]))
    for b in r["per_building"]:
        print("  {}: {:.0f} kWh, {:.1f} tCO2e".format(b["building"], b["kwh"], b["co2_t"]))
    print("\n" + esg_summary())
