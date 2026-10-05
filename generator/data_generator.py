# -*- coding: utf-8 -*-
"""
慧碳安居 · 数据生成器 v2（真实化重构）
----------------------------------------
v1 的问题（用户与行业双重验证）：
- 噪声为独立同分布 4%，负荷过于光滑，MAPE 4.51% 远低于真实建筑数据 8-15%（Building Health X 曾指出
  "合成数据预测误差过低反而暴露造假"）
- 异常为固定 +80kW、固定时段，幅度 80kW/峰值150kW ≈ 53%，过于明显；检测 F1=1.0 在真实场景不可能

v2 真实化改造：
1. 温度：季节 + 昼夜 + 噪声，并生成"热惯性"温度（前3小时加权）供空调负荷使用
2. 负荷：
   - 异方差噪声：白天人/设备活动多，波动大；夜间稳定（sigma 随时段形状变化）
   - AR(1) 自相关噪声：真实负荷相邻小时相关，而非白噪声
   - 逐日漂移：每天基准负荷随机偏移 ±3%（入住率/运营变化）
3. 光伏：按天天气马尔可夫链（今天晴明天大概率仍晴，天气有持续性），日内叠加云遮抖动
4. 三类隐蔽异常（幅度 5%~17%，模拟真实设备问题）：
   - A型·设备效率退化（渐变 drift）：A栋办公楼空调系统 COP 下降，自第55天起空调负荷每天 +0.5%，累计 +17%
   - B型·下班后待机未关（间歇）：C栋商业楼部分周末 20-23 时空调/照明未关（非每个周末都发生）
   - C型·基荷漂移（baseline drift）：B栋住宅深夜公共照明/设备老化，自第60天起基荷每天 +0.3%，累计 +9%

ground truth 保留 is_anomaly 标签，供算法评估；异常幅度刻意接近噪声水平，检测不应 100%。
"""
import os
import sys
import sqlite3
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# ---------- 日负荷形状模板（相对值，0~1） ----------
OFFICE = np.array([
    0.30, 0.28, 0.26, 0.25, 0.25, 0.30,
    0.45, 0.70, 0.88, 0.95, 0.92, 0.90,
    0.88, 0.85, 0.88, 0.92, 1.00, 0.98,
    0.92, 0.85, 0.75, 0.65, 0.55, 0.45,
])
RESIDENTIAL = np.array([
    0.35, 0.32, 0.30, 0.28, 0.30, 0.38,
    0.55, 0.70, 0.75, 0.80, 0.78, 0.72,
    0.70, 0.68, 0.72, 0.80, 0.90, 1.00,
    0.98, 0.92, 0.82, 0.70, 0.58, 0.45,
])
COMMERCIAL = np.array([
    0.20, 0.18, 0.17, 0.16, 0.18, 0.22,
    0.30, 0.45, 0.70, 0.88, 0.95, 0.98,
    1.00, 0.98, 0.95, 0.98, 1.00, 0.98,
    0.92, 0.85, 0.75, 0.65, 0.50, 0.35,
])
PROFILES = {"office": OFFICE, "residential": RESIDENTIAL, "commercial": COMMERCIAL}

# ---------- 异常埋设参数（v2） ----------
# A型：A栋办公，空调效率退化，day>=65 起，下午空调时段每天 +0.4%（累计约+10%）
DRIFT_A = {"building": "A", "start_day": 65, "daily_rate": 0.004,
           "hours": range(12, 19), "desc": "A栋空调系统COP下降（设备效率渐变退化）"}
# B型：C栋商业，部分周末 20-23 时未关空调/照明（第0、4个周末日训练期，第17、18个周末日检测期；
#    ×1.30：商业楼晚间"该关未关"应保持营业空调水平，显著高于节能基线（真实物理）
DRIFT_B = {"building": "C", "weekend_idx": [0, 4, 17, 18], "hours": range(20, 24),
           "desc": "C栋部分周末下班后空调/照明未关（间歇性）"}
# C型：B栋住宅，深夜基荷漂移，day>=70 起每天 +0.4%（累计约+8%）
DRIFT_C = {"building": "B", "start_day": 70, "daily_rate": 0.004,
           "hours": range(0, 6), "desc": "B栋深夜公共照明/设备老化（基荷缓慢漂移）"}


def gen_temperature_raw(day_idx, hour, rng):
    """基础温度：季节 + 昼夜 + 噪声（℃）"""
    season = 28 + 3 * np.cos(2 * np.pi * day_idx / 120)
    diurnal = 5 * np.sin(2 * np.pi * (hour - 8) / 24)
    noise = rng.normal(0, 1.0)
    return season + diurnal + noise


def gen_weather_chain(n_days, seed=7):
    """按天天气马尔可夫链：0晴 / 1多云 / 2阴；状态转移矩阵体现天气持续性
    转移概率（经验设定，符合"晴天持续、阴天转晴较快"的天气规律）：
    P: [[0.75, 0.20, 0.05],
        [0.40, 0.45, 0.15],
        [0.35, 0.30, 0.35]]
    天气系数：晴1.0 / 多云0.65 / 阴0.30
    """
    rng = np.random.RandomState(seed)
    states = np.zeros(n_days, dtype=int)
    states[0] = 0
    P = np.array([[0.75, 0.20, 0.05],
                  [0.40, 0.45, 0.15],
                  [0.35, 0.30, 0.35]])
    for d in range(1, n_days):
        states[d] = rng.choice(3, p=P[states[d - 1]])
    coef = {0: 1.0, 1: 0.65, 2: 0.30}
    return np.array([coef[s] for s in states])


def gen_pv_hourly(day_idx, hour, pv_capacity_kw, day_weather_coef, rng):
    """光伏出力 v2（kW）：
    日发电量 = 装机 × 等效峰值日照 × 天气系数 × 系统效率（依据同v1）
    日内形状 sin²，但叠加短时云遮抖动（多云/阴天更明显），模拟真实出力锯齿
    """
    if hour < 5 or hour > 19:
        return 0.0
    season_h = 4.8 + 0.6 * np.cos(2 * np.pi * day_idx / 120)
    eff_hours = max(season_h * day_weather_coef, 0.2)
    day_energy = pv_capacity_kw * eff_hours * 0.85
    shape = np.sin(np.pi * (hour - 5) / 14) ** 2
    total_shape = sum(np.sin(np.pi * (h - 5) / 14) ** 2 for h in range(5, 19))
    # 云遮抖动：多云天 ±15%，阴天 ±30%，晴天 ±5%（高频小扰动）
    cloud_jitter = {1.0: 0.05, 0.65: 0.15, 0.30: 0.30}[day_weather_coef]
    jitter = 1.0 + rng.normal(0, cloud_jitter)
    return round(day_energy * shape / total_shape * max(jitter, 0.4), 2)


def price_of_hour(hour):
    """按山东2026年9-11月时段返回电价（元/kWh）"""
    for a, b in config.TOU_SCHEDULE["sharp"]:
        if a <= hour < b:
            return config.PRICE_SHARP
    for a, b in config.TOU_SCHEDULE["peak"]:
        if a <= hour < b:
            return config.PRICE_PEAK
    for a, b in config.TOU_SCHEDULE["deep"]:
        if a <= hour < b:
            return config.PRICE_DEEP
    for a, b in config.TOU_SCHEDULE["valley"]:
        if a <= hour < b:
            return config.PRICE_VALLEY
    return config.PRICE_FLAT


def generate(rebuild=True):
    """生成 v2 数据并写入 SQLite；返回 (df, ground_truth_df)"""
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy.db")
    if rebuild and os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS buildings (
        id TEXT PRIMARY KEY, name TEXT, type TEXT, area_m2 REAL,
        annual_kwh_per_m2 REAL, base_kwh_day REAL, peak_power_kw REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS hourly_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, day_idx INT, hour INT,
        building TEXT, load_kw REAL, pv_kw REAL,
        price REAL, temp_c REAL, is_workday INT,
        is_anomaly INT, anomaly_desc TEXT)""")
    cur.execute("""CREATE INDEX IF NOT EXISTS idx_building_ts ON hourly_records (building, ts)""")

    for b in config.BUILDINGS:
        cur.execute("INSERT INTO buildings VALUES (?,?,?,?,?,?,?)",
                    (b["id"], b["name"], b["type"], b["area_m2"],
                     b["annual_kwh_per_m2"], b["base_kwh_day"], b["peak_power_kw"]))

    np.random.seed(config.SEED)
    dates = pd.date_range(start=config.START_DATE, periods=config.N_DAYS, freq="D")
    weather = gen_weather_chain(config.N_DAYS)

    # 周末判定：周六(5)/周日(6) 均为周末；B型异常作用于周末序号列表（体现"非每次周末都发生"）
    weekend_map = {}
    wk_idx = 0
    for d in range(config.N_DAYS):
        if dates[d].weekday() >= 5:  # 周六或周日
            weekend_map[d] = wk_idx
            wk_idx += 1

    rows = []
    # 用于 AR(1) 噪声：每个楼栋记录上一小时噪声
    prev_noise = {b["id"]: 0.0 for b in config.BUILDINGS}
    temp_history = {b["id"]: [] for b in config.BUILDINGS}  # 每楼栋热惯性温度序列

    for d_idx, date in enumerate(dates):
        is_workday = 1 if date.weekday() < 5 else 0
        day_drift = np.random.normal(0, 0.03)  # 逐日基准漂移 ±3%
        for hour in range(24):
            for b in config.BUILDINGS:
                profile = PROFILES[b["load_profile"]]
                base = b["base_kwh_day"] / 24.0
                rng = np.random.RandomState(d_idx * 1000 + hour * 10 + {"A": 0, "B": 1, "C": 2}[b["id"]])

                # 1) 温度（含热惯性：空调响应前3小时加权温度，模拟建筑蓄热）
                temp_now = gen_temperature_raw(d_idx, hour, rng)
                temp_history[b["id"]].append(temp_now)
                h = temp_history[b["id"]]
                if len(h) >= 3:
                    temp_eff = 0.5 * h[-1] + 0.3 * h[-2] + 0.2 * h[-3]
                else:
                    temp_eff = temp_now
                ac = 0.08 * (temp_eff - 24) if temp_eff > 24 else 0.0

                # 2) 工作日系数（同v1）
                if b["load_profile"] == "office":
                    wd_factor = 1.0 if is_workday else 0.45
                elif b["load_profile"] == "commercial":
                    wd_factor = 1.0 if is_workday else 0.75
                else:
                    wd_factor = 0.95 if is_workday else 1.10

                # 3) 确定性负荷
                load = base * profile[hour] * wd_factor * (1 + ac) * (1 + day_drift)

                # 4) 异方差 + AR(1) 自相关噪声：sigma 随时段（白天大夜间小）
                #    AR(1) 稳态方差 = σ²/(1-ρ²)，ρ=0.7 时放大 1.96 倍，故基础 sigma 取小值
                sigma = 0.010 + 0.020 * profile[hour]
                eps = rng.normal(0, sigma)
                noise_t = 0.7 * prev_noise[b["id"]] + eps
                prev_noise[b["id"]] = noise_t
                load *= (1 + noise_t)

                # 5) 峰值限幅
                load = min(load, b["peak_power_kw"])

                # 6) 埋入三类隐蔽异常
                is_anom = 0
                desc = ""
                # A型：A栋办公空调效率退化（渐变）
                if (b["id"] == DRIFT_A["building"] and d_idx >= DRIFT_A["start_day"]
                        and hour in DRIFT_A["hours"]):
                    degrade = 1 + DRIFT_A["daily_rate"] * (d_idx - DRIFT_A["start_day"])
                    load *= degrade
                    is_anom = 1
                    desc = DRIFT_A["desc"]
                # B型：C栋商业部分周末未关空调（间歇）
                elif (b["id"] == DRIFT_B["building"] and not is_workday
                        and d_idx in weekend_map and weekend_map[d_idx] in DRIFT_B["weekend_idx"]
                        and hour in DRIFT_B["hours"]):
                    load *= 1.30  # 未关设备：保持营业空调水平，比节能基线高30%
                    is_anom = 1
                    desc = DRIFT_B["desc"]
                # C型：B栋住宅深夜基荷漂移（渐变）
                elif (b["id"] == DRIFT_C["building"] and d_idx >= DRIFT_C["start_day"]
                        and hour in DRIFT_C["hours"]):
                    drift = 1 + DRIFT_C["daily_rate"] * (d_idx - DRIFT_C["start_day"])
                    load *= drift
                    is_anom = 1
                    desc = DRIFT_C["desc"]

                # 7) 光伏 + 电价 + 入库
                pv = gen_pv_hourly(d_idx, hour, b["pv_capacity_kw"], weather[d_idx], rng)
                price = price_of_hour(hour)
                ts = date.strftime("%Y-%m-%d") + f" {hour:02d}:00:00"
                rows.append((ts, d_idx, hour, b["id"], round(load, 2), pv,
                             price, round(temp_now, 1), is_workday, is_anom, desc))

    cur.executemany("INSERT INTO hourly_records (ts, day_idx, hour, building, load_kw, pv_kw, price, temp_c, is_workday, is_anomaly, anomaly_desc) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    df = pd.DataFrame(rows, columns=["ts", "day_idx", "hour", "building", "load_kw",
                                     "pv_kw", "price", "temp_c", "is_workday",
                                     "is_anomaly", "anomaly_desc"])
    gt = df[df["is_anomaly"] == 1].copy()
    print(f"[生成器v2] 共 {len(df)} 条记录 → energy.db")
    print(f"[生成器v2] 埋入异常 {len(gt)} 条（{gt['anomaly_desc'].nunique()} 类）")
    print(gt["anomaly_desc"].value_counts().to_string())
    return df, gt


if __name__ == "__main__":
    df, gt = generate()
    print("日均用电量(kWh):", round(df.groupby(df["ts"].str[:10])["load_kw"].sum().mean(), 1))
