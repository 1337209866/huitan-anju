# -*- coding: utf-8 -*-
"""
慧碳安居 · 异常检测模块 v2（真实化重构）
----------------------------------------
算法选型：预测残差法（工业界故障检测与诊断 FDD 的标准范式，IEEE 综述/多项专利一致）
- 核心思想：训练一个"期望能耗"基线模型（只依赖外生因素：时段/温度/光伏），
  实际负荷与期望值的偏差（残差）即为异常信号。
  关键设计：模型特征【不包含 lag 滞后项】——若包含 lag，模型会"跟着异常走"，
  把异常当天的高负荷当作历史惯性，残差被抹平，渐变型异常将完全漏检。
- 双路检测：
  ① 瞬时残差阈值（|标准残差| > 3.5σ）→ 抓突变/间歇型异常
  ② 日×时段 CUSUM 累积（对渐变 drift 敏感：单点残差小但连续多日
     系统性正偏会被累积放大）→ 抓渐变型异常
- 两阶段鲁棒训练：先拟合初版模型 → 剔除训练期疑似异常样本 →
  干净数据重拟合，避免训练期未知异常污染基线（工业界"清洗后建模"标准做法）
- 与 MAD 统计基线（无预测模型，纯分组中位数偏差）对比，输出精确率/召回率/F1

异常定义（ground truth，v2 三类隐蔽异常）：
  A型·渐变退化：A栋空调 COP 下降，第65天起空调负荷每天+0.4%，累计约+10%（残差缓慢正向漂移）
  B型·间歇未关：C栋部分周末 20-23 时未关空调，比节能基线高30%（第0/4/17/18个周末日）
  C型·基荷漂移：B栋深夜公共照明老化，第70天起基荷每天+0.4%，累计约+8%
"""
import os
import sys
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import precision_recall_fscore_support

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy.db")

# 期望模型特征：仅外生变量（不含 lag，见模块说明）
F_FEATURES = ["hour", "dow", "is_workday", "temp_c", "pv_kw"]
BUILDINGS = ["A", "B", "C"]

# 双路阈值
K_INSTANT = 3.5        # 瞬时残差标准阈值（3.5σ）
EWMA_LAMBDA = 0.15     # EWMA 平滑系数（越小越平滑，对渐变越敏感）
K_EWMA = 2.0           # EWMA 标准残差阈值


def load_data():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM hourly_records", conn)
    conn.close()
    df["dt"] = pd.to_datetime(df["ts"])
    df["dow"] = df["dt"].dt.dayofweek
    df["hour"] = df["hour"].astype(int)
    df["is_workday"] = df["is_workday"].astype(int)
    return df


def build_features(df):
    return df.copy()


def train_expected_model(train_df, robust=True):
    """逐楼栋训练"期望能耗"LightGBM 模型（仅外生特征）

    robust=True 时采用两阶段鲁棒训练（工业界清洗后建模）：
      第1阶段：初版模型拟合 → 计算训练期残差 → 剔除 |z|>2.5 的疑似异常样本
      第2阶段：用干净样本重拟合
    目的：训练期历史数据中可能混有未知异常（如早期的间歇事件），
    若直接建模，模型会把异常模式"当正常学会"，压低真实异常的残差信号。
    返回 (models, clean_train_df)：clean_train_df 为清洗后的训练数据，
    供下游标准化尺度与阈值估计使用（保证阈值不被污染样本抬高）。
    """
    models = {}
    clean_frames = []
    for b in BUILDINGS:
        tb = train_df[train_df["building"] == b]
        if len(tb) < 200:
            continue
        # 第1阶段：初版模型
        m0 = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1)
        m0.fit(tb[F_FEATURES], tb["load_kw"])
        if not robust:
            models[b] = m0
            clean_frames.append(tb)
            continue
        # 残差鲁棒筛选：按 楼栋×小时×工作日 分组 MAD 标准化，剔除 |z|>2.5
        tb = tb.copy()
        tb["resid"] = tb["load_kw"] - m0.predict(tb[F_FEATURES])
        med = tb.groupby(["hour", "is_workday"])["resid"].transform("median")
        mad = tb.groupby(["hour", "is_workday"])["resid"].transform(
            lambda x: np.median(np.abs(x - np.median(x))) + 1e-9)
        tb["z"] = (tb["resid"] - med) / (1.4826 * mad)
        clean = tb[tb["z"].abs() <= 2.5]
        # 第2阶段：干净样本重拟合
        m = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1)
        m.fit(clean[F_FEATURES], clean["load_kw"])
        models[b] = m
        clean_frames.append(clean)
    clean_train = pd.concat(clean_frames, ignore_index=True) if clean_frames else train_df
    return models, clean_train


def standardize_residual(group):
    """按 楼栋×小时×工作日 分组做鲁棒标准化：z = (r - med) / (1.4826 * MAD)"""
    med = group["hour"].map(group.groupby("hour")["resid"].transform("median"))
    mad = group["hour"].map(group.groupby("hour")["resid"].apply(
        lambda x: np.median(np.abs(x - np.median(x))) + 1e-9))
    group["z"] = (group["resid"] - med) / (1.4826 * mad)
    return group


def residual_detection(df, models, clean_train=None, train_until=60,
                       k_inst=3.5, cusum_k=0.5, cusum_h=3.5, cusum_min_run=2):
    """预测残差法双路检测；返回带 pred_anomaly 标记的 DataFrame

    train_until：训练窗口结束 day_idx（<train_until 为基线期）。
    clean_train：两阶段鲁棒训练产出的干净训练数据（用于估计标准化尺度与
       CUSUM 基线 μ0/σ）。若为 None 则回退用全部训练数据。
    双路设计（工业界 EMS 两层告警 + 统计过程控制范式）：
    ① 小时级瞬时路：|标准残差 z| > k_inst（MAD 鲁棒尺度下的固定阈值）。
       z 由干净训练数据的残差中位数/MAD 标准化，间歇型异常（如周末
       该关未关）在此路体现为小时级强偏差。
    ② 日×时段 CUSUM 路：每楼栋按时段（白天8-17/傍晚18-23/深夜0-7）聚合
       成逐日残差均值序列，对序列做单侧 CUSUM 累积和检测（正向偏移=多耗电）。
       渐变漂移的特点是"每天只偏一点点、但连续多天持续正偏"，
       单日阈值法会被噪声淹没，而 CUSUM 把微小偏移连续累积，灵敏度远高于
       单点阈值；同时日级聚合消除了小时级白天/夜间正负震荡对累积的干扰。
    防误报设计：CUSUM 需连续 cusum_min_run 天超阈值才标记（单日偶发
       超阈不报警，过滤天气等外部因素的偶发扰动）。
    参数：k=0.5σ（允许的噪声滑移量），h=3.5σ（触发阈值），经典 CUSUM 参数。
    """
    # 时段划分（真实 EMS 常用：工作时段/傍晚/深夜）
    def seg_of(h):
        if 8 <= h <= 17:
            return "白天"
        if 18 <= h <= 23:
            return "傍晚"
        return "深夜"

    g = df.copy()
    g["segment"] = g["hour"].map(seg_of)
    # clean_train 也补 segment 列（来自训练阶段，可能未带）
    if clean_train is not None:
        clean_train = clean_train.copy()
        clean_train["segment"] = clean_train["hour"].map(seg_of)
    # 期望值预测
    g["expected"] = 0.0
    for b, m in models.items():
        idx = g["building"] == b
        g.loc[idx, "expected"] = m.predict(g.loc[idx, F_FEATURES])
    g["resid"] = g["load_kw"] - g["expected"]

    # 标准化尺度：用干净训练数据（若未提供则用全部训练数据）估计
    # 楼栋×小时×工作日 的残差中位数/MAD
    base = clean_train if clean_train is not None else g[g["day_idx"] < train_until]
    scale = base.groupby(["building", "hour", "is_workday"])["resid"].agg(
        ["median", lambda x: np.median(np.abs(x - np.median(x))) + 1e-9])
    scale.columns = ["med", "mad"]
    g = g.merge(scale, left_on=["building", "hour", "is_workday"],
                right_index=True, how="left")
    g["z"] = (g["resid"] - g["med"]) / (1.4826 * g["mad"])

    train_mask = g["day_idx"] < train_until
    tr = g[train_mask]

    # 路1：小时级瞬时 —— MAD 鲁棒尺度下的固定 z 阈值
    g["thr_inst"] = k_inst
    g["flag_instant"] = (g["z"].abs() > g["thr_inst"]).astype(int)

    # 路2：日×时段 CUSUM —— 抓渐变型漂移，连续多天超阈才标记
    g["flag_cusum"] = 0
    for b in BUILDINGS:
        for seg in ["白天", "傍晚", "深夜"]:
            # 基线期：该楼栋该时段逐日残差均值 → μ0 与 σ（鲁棒）
            base_bs = base[(base["building"] == b) & (base["segment"] == seg)]
            if len(base_bs) < 20:
                continue
            daily_tr = base_bs.groupby("day_idx")["resid"].mean()
            mu0 = daily_tr.median()
            sigma = 1.4826 * np.median(np.abs(daily_tr - mu0)) + 1e-9
            # 段均值显著性门槛：触发后仅标记段均值显著高于基线的日子
            # （避免"持续告警"把仅轻微偏高的正常点也全标为异常）
            seg_med = daily_tr.median()
            seg_mad = 1.4826 * np.median(np.abs(daily_tr - seg_med)) + 1e-9
            min_seg_mean = seg_med + 0.5 * seg_mad
            # 检测期：逐日更新单侧 CUSUM，记录每日是否超阈
            b_df = g[(g["building"] == b) & (g["segment"] == seg)]
            days = sorted(b_df["day_idx"].unique())
            S = 0.0
            over = {}   # day -> 是否超阈
            for d in days:
                x = b_df[b_df["day_idx"] == d]["resid"].mean()
                S = max(0.0, S + (x - mu0 - cusum_k * sigma))
                over[d] = (S > cusum_h * sigma)
            # 连续 cusum_min_run 天超阈 → 从第 cusum_min_run 天起，
            # 标记"段均值显著且 resid>0"的点
            streak = 0
            for d in days:
                if over.get(d, False):
                    streak += 1
                else:
                    streak = 0
                if streak >= cusum_min_run and d >= train_until:
                    x = b_df[b_df["day_idx"] == d]["resid"].mean()
                    if x <= min_seg_mean:
                        continue
                    mask = ((g["building"] == b) & (g["segment"] == seg)
                            & (g["day_idx"] == d) & (g["resid"] > 0))
                    g.loc[mask, "flag_cusum"] = 1

    g["pred_anomaly"] = ((g["flag_instant"] == 1) | (g["flag_cusum"] == 1)).astype(int)
    return g


def mad_baseline(df, k=3.5):
    """对比基线：MAD 统计法（无预测模型）
    同一楼栋×同一小时×同一工作日属性分组，|x-中位数| > k*MAD 判定异常。
    """
    res = []
    for (building, hour, is_wd), group in df.groupby(["building", "hour", "is_workday"]):
        med = group["load_kw"].median()
        mad = (group["load_kw"] - med).abs().median() * 1.4826
        thr = k * mad if mad > 1e-6 else k * 1.0
        g = group.copy()
        g["pred_anomaly"] = ((group["load_kw"] - med).abs() > thr).astype(int)
        res.append(g)
    return pd.concat(res, ignore_index=True)


def evaluate(df, pred_col="pred_anomaly"):
    """与 ground truth 对比，输出指标（全局评估）"""
    p, r, f1, _ = precision_recall_fscore_support(
        df["is_anomaly"], df[pred_col], average="binary", zero_division=0)
    tp = ((df[pred_col] == 1) & (df["is_anomaly"] == 1)).sum()
    fp = ((df[pred_col] == 1) & (df["is_anomaly"] == 0)).sum()
    fn = ((df[pred_col] == 0) & (df["is_anomaly"] == 1)).sum()
    return {"precision": round(float(p), 4), "recall": round(float(r), 4),
            "f1": round(float(f1), 4), "tp": int(tp), "fp": int(fp), "fn": int(fn),
            "total_gt": int(df["is_anomaly"].sum())}


def per_type_recall(df, pred_col="pred_anomaly"):
    """三类异常的分型检出率（召回率）"""
    out = {}
    for desc, g in df[df["is_anomaly"] == 1].groupby("anomaly_desc"):
        hit = int((g[pred_col] == 1).sum())
        out[desc] = {"gt": int(len(g)), "hit": hit,
                     "recall": round(hit / len(g), 4) if len(g) else 0}
    return out


def quantify_waste(df, pred_col="pred_anomaly"):
    """异常浪费电量与金额：对比预测期望值（残差>0 部分即多耗的电）"""
    anom = df[(df[pred_col] == 1) & (df["resid"] > 0)].copy()
    if len(anom) == 0:
        return {"waste_kwh": 0.0, "waste_cost": 0.0, "anomaly_count": 0}
    waste_kwh = anom["resid"].sum()
    waste_cost = (anom["resid"] * anom["price"]).sum()
    return {"waste_kwh": round(float(waste_kwh), 1),
            "waste_cost": round(float(waste_cost), 2),
            "anomaly_count": int(len(anom))}


def run():
    df = load_data()
    TRAIN_UNTIL = 60
    # 训练/检测划分：前60天训练期望模型（异常集中在65天后，训练窗口基本干净），
    # 评估只在检测窗口（day>=60，模拟"模型部署后持续监测新数据"）
    train_df = df[df["day_idx"] < TRAIN_UNTIL]
    models, clean_train = train_expected_model(train_df)
    res = residual_detection(df, models, clean_train=clean_train,
                             train_until=TRAIN_UNTIL)

    mad = mad_baseline(df)
    # 评估仅限检测窗口（用索引位置避免 reindex 警告）
    eval_mask = (df["day_idx"] >= TRAIN_UNTIL).values
    res_eval = res[eval_mask].copy()
    res_eval = res_eval.reset_index(drop=True)
    mad_eval = mad[eval_mask].copy()
    res_metrics = evaluate(res_eval)
    mad_metrics = evaluate(mad_eval)
    res_waste = quantify_waste(res_eval)
    types = per_type_recall(res_eval)

    hits = res_eval[res_eval["pred_anomaly"] == 1][
        ["ts", "building", "load_kw", "expected", "price"]]
    hits = hits.sort_values("ts")
    hits = hits.merge(df[["ts", "building", "is_anomaly"]].rename(
        columns={"is_anomaly": "is_gt"}),
        on=["ts", "building"], how="left")

    return {
        "residual": res_metrics, "mad": mad_metrics,
        "waste": res_waste, "types": types,
        "hits": hits.head(60).to_dict("records"),
        "total_records": int(len(df)),
        "eval_records": int(len(res_eval)),
        "k_instant": K_INSTANT, "k_ewma": K_EWMA,
        "cusum_k": 0.5, "cusum_h": 3.5, "train_until": TRAIN_UNTIL,
    }


if __name__ == "__main__":
    r = run()
    print("=== 预测残差法（v2，双路：瞬时 + 日级CUSUM） ===")
    m = r["residual"]
    print("精确率 {:.2%} 召回率 {:.2%} F1 {:.3f} | TP={} FP={} FN={} (GT={})".format(
        m["precision"], m["recall"], m["f1"], m["tp"], m["fp"], m["fn"], m["total_gt"]))
    print("=== MAD 统计基线对比 ===")
    mb = r["mad"]
    print("精确率 {:.2%} 召回率 {:.2%} F1 {:.3f}".format(
        mb["precision"], mb["recall"], mb["f1"]))
    print("=== 三类异常分型检出 ===")
    for desc, t in r["types"].items():
        print("  {}: GT={} 命中={} 召回 {:.1%}".format(desc, t["gt"], t["hit"], t["recall"]))
    print("=== 浪费量化 ===")
    print("异常浪费电量 {:.0f} kWh, 折合电费 ¥{:.0f}".format(
        r["waste"]["waste_kwh"], r["waste"]["waste_cost"]))
