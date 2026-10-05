# -*- coding: utf-8 -*-
"""
慧碳安居 · 负荷预测模块
--------------------------------
算法选型：LightGBM（梯度提升树）
- 表格时序预测的主流强模型，对小时级负荷非线性关系拟合好、训练快
- 特征：小时、星期、工作日、温度、光伏出力、昨日同时刻负荷、前1小时负荷
- 逐楼栋独立建模（三栋楼负荷形态差异大，分栋训练更准）
- 时间窗：前 75 天训练 → 后 15 天测试（模拟"用历史预测未来"）
- 指标：RMSE、MAE、MAPE（工程常用预测误差口径）
"""
import os
import sys
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import mean_absolute_error, mean_squared_error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "energy.db")
FEATURES = ["hour", "dow", "is_workday", "temp_c", "pv_kw", "lag24", "lag1"]
BUILDINGS = ["A", "B", "C"]


def load_data():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM hourly_records", conn)
    conn.close()
    df["dt"] = pd.to_datetime(df["ts"])
    df["dow"] = df["dt"].dt.dayofweek
    return df


def build_features(df):
    df = df.sort_values(["building", "dt"]).copy()
    g = df.groupby("building", group_keys=False)
    df["lag24"] = g["load_kw"].shift(24)    # 昨日同时刻
    df["lag1"] = g["load_kw"].shift(1)      # 前一小时
    df["hour"] = df["hour"].astype(int)
    df["is_workday"] = df["is_workday"].astype(int)
    return df


def prepare():
    """返回按楼栋切分好的 train/test"""
    df = build_features(load_data()).dropna(subset=FEATURES).copy()
    test_start = df["day_idx"].max() - 15
    train, test = df[df["day_idx"] < test_start], df[df["day_idx"] >= test_start]
    return train, test


def train_models(train_df):
    """逐楼栋训练 LightGBM，返回 {building: model}"""
    models = {}
    for b in BUILDINGS:
        tb = train_df[train_df["building"] == b]
        if len(tb) < 100:
            continue
        m = lgb.LGBMRegressor(
            n_estimators=500, learning_rate=0.05, num_leaves=31,
            subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1)
        m.fit(tb[FEATURES], tb["load_kw"])
        models[b] = m
    return models


def evaluate():
    train, test = prepare()
    models = train_models(train)
    rmse, mae, mape = 0.0, 0.0, 0.0
    n_test = 0
    per_building = {}
    for b, m in models.items():
        tb = test[test["building"] == b]
        y_true, y_pred = tb["load_kw"].values, m.predict(tb[FEATURES])
        rmse += float(np.sqrt(mean_squared_error(y_true, y_pred))) * len(y_true)
        mae += float(mean_absolute_error(y_true, y_pred)) * len(y_true)
        mape += float(np.mean(np.abs((y_true - y_pred) / np.maximum(y_true, 1e-6)))) * 100 * len(y_true)
        n_test += len(y_true)
        per_building[b] = {
            "mape": round(float(np.mean(np.abs((y_true - y_pred) / np.maximum(y_true, 1e-6)))) * 100, 2),
            "mae": round(float(mean_absolute_error(y_true, y_pred)), 1),
        }
    return {
        "rmse": round(rmse / n_test, 2), "mae": round(mae / n_test, 2),
        "mape": round(mape / n_test, 2),
        "per_building": per_building,
        "n_train": int(len(train)), "n_test": int(n_test),
        "models": models,
    }


def predict_future(models, hours=24):
    """滚动预测未来 hours 小时园区总负荷（逐楼栋预测后求和）"""
    df = build_features(load_data()).dropna(subset=FEATURES).copy()
    last_dt = df["dt"].max()
    # 历史同期参考（温度/光伏的均值近似）
    hist = df.copy()
    total = None
    for b in BUILDINGS:
        if b not in models:
            continue
        bdf = hist[hist["building"] == b]
        preds = []
        prev = None
        for step in range(1, hours + 1):
            ft = last_dt + pd.Timedelta(hours=step)
            same_hour = bdf[bdf["dt"].dt.hour == ft.hour]
            temp = same_hour["temp_c"].mean() if len(same_hour) else 28.0
            pv = same_hour["pv_kw"].mean() if len(same_hour) else 0.0
            lag24_row = bdf[bdf["dt"] == ft - pd.Timedelta(hours=24)]
            lag24 = lag24_row["load_kw"].values[0] if len(lag24_row) else 100.0
            lag1 = prev if prev is not None else (
                bdf[bdf["dt"] == ft - pd.Timedelta(hours=1)]["load_kw"].values[0]
                if len(bdf[bdf["dt"] == ft - pd.Timedelta(hours=1)]) else 100.0)
            row = pd.DataFrame([{
                "hour": ft.hour, "dow": ft.dayofweek,
                "is_workday": int(ft.weekday() < 5),
                "temp_c": temp, "pv_kw": pv, "lag24": lag24, "lag1": lag1,
            }])[FEATURES]
            p = float(models[b].predict(row)[0])
            preds.append(p)
            prev = p
        if total is None:
            total = preds
        else:
            total = [x + y for x, y in zip(total, preds)]
    hours_label = [(last_dt + pd.Timedelta(hours=i + 1)).strftime("%m-%d %H:00")
                   for i in range(hours)]
    return [{"time": h, "load_kw": round(v, 1)}
            for h, v in zip(hours_label, total)]


if __name__ == "__main__":
    r = evaluate()
    print("=== LightGBM 负荷预测（逐楼栋建模） ===")
    print("训练 {} 条 / 测试 {} 条".format(r["n_train"], r["n_test"]))
    print("RMSE {:.2f} kW | MAE {:.2f} kW | MAPE {:.2f}%".format(
        r["rmse"], r["mae"], r["mape"]))
    for b, m in r["per_building"].items():
        print("  {}: MAPE {:.2f}% | MAE {:.1f} kW".format(b, m["mape"], m["mae"]))
