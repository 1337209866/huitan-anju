# -*- coding: utf-8 -*-
"""
慧碳安居 · Flask 后端
--------------------------------
数据源：energy.db（SQLite，数据生成器产出）
算法：algo/ 四模块（异常检测/负荷预测/储能调度/碳核算）
接口：REST JSON 供前端 ECharts 渲染
"""
import os
import sys
import sqlite3
import json
import traceback
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from algo import anomaly, forecast, storage, carbon
from agent import assistant

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "energy.db")

app = Flask(__name__)


# ---------------- 数据查询工具 ----------------
def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- 页面路由 ----------------
@app.route("/")
def page_overview():
    return render_template("overview.html")


@app.route("/energy")
def page_energy():
    return render_template("energy.html")


@app.route("/anomaly")
def page_anomaly():
    return render_template("anomaly.html")


@app.route("/forecast")
def page_forecast():
    return render_template("forecast.html")


@app.route("/storage")
def page_storage():
    return render_template("storage.html")


@app.route("/carbon")
def page_carbon():
    return render_template("carbon.html")


@app.route("/chat")
def page_chat():
    return render_template("chat.html")


# ---------------- 对话助手 ----------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = (data.get("message") or "").strip()
        reply, intent = assistant.ask(msg)
        return jsonify({"reply": reply, "intent": intent})
    except Exception as e:
        return jsonify({"reply": f"助手出错：{e}", "intent": "error"}), 200


# ---------------- 总览 ----------------
@app.route("/api/overview")
def api_overview():
    """总览卡片 + 近30天用电/碳排趋势 + 楼栋占比"""
    df = query("SELECT * FROM hourly_records ORDER BY ts")
    # 汇总
    total_kwh = sum(r["load_kw"] for r in df)
    total_pv = sum(r["pv_kw"] for r in df)
    co2 = total_kwh * config.CARBON_FACTOR / 1000.0
    n_days = len(set(r["ts"][:10] for r in df))
    # 按日聚合
    daily = {}
    for r in df:
        d = r["ts"][:10]
        daily.setdefault(d, {"kwh": 0.0, "pv": 0.0, "co2": 0.0})
        daily[d]["kwh"] += r["load_kw"]
        daily[d]["pv"] += r["pv_kw"]
        daily[d]["co2"] += r["load_kw"] * config.CARBON_FACTOR / 1000.0
    days = sorted(daily.keys())
    last30 = days[-30:]
    trend = [{"date": d, "kwh": round(daily[d]["kwh"], 1),
              "pv": round(daily[d]["pv"], 1),
              "co2": round(daily[d]["co2"], 2)} for d in last30]
    # 楼栋占比（用电）
    by_b = {}
    for r in df:
        by_b.setdefault(r["building"], 0.0)
        by_b[r["building"]] += r["load_kw"]
    buildings = query("SELECT * FROM buildings ORDER BY id")
    b_list = []
    for b in buildings:
        kwh = by_b.get(b["id"], 0.0)
        # 年化单耗：90天为夏季制冷季（7-9月），空调负荷高于全年均值，
        # 折年化时除以夏季系数1.25（与生成器自检口径一致）
        b_list.append({"name": b["name"], "id": b["id"], "type": b["type"], "area": b["area_m2"],
                       "kwh": round(kwh, 1),
                       "co2": round(kwh * config.CARBON_FACTOR / 1000.0, 2),
                       "annual_per_m2": round(kwh / b["area_m2"] * 365 / n_days / 1.25, 1)})
    # 峰值
    peak = max((r["load_kw"] for r in df), default=0)
    return jsonify({
        "total_kwh": round(total_kwh, 1), "total_pv": round(total_pv, 1),
        "total_co2": round(co2, 2), "n_days": n_days, "peak_kw": round(peak, 1),
        "factor": config.CARBON_FACTOR, "trend": trend, "buildings": b_list,
        "dates": days,
    })


# ---------------- 能耗分析 ----------------
@app.route("/api/energy")
def api_energy():
    """楼栋切换：24h典型曲线 / 周对比 / 工作日vs周末 / 温度关联"""
    b = request.args.get("building", "A")
    # 楼栋逐时曲线（全周期按小时平均）
    df = query("SELECT hour, load_kw, pv_kw, price, temp_c, is_workday FROM hourly_records WHERE building=?",
               (b,))
    by_hour = {}
    for r in df:
        h = r["hour"]
        by_hour.setdefault(h, {"load": [], "pv": [], "price": [], "temp": []})
        by_hour[h]["load"].append(r["load_kw"])
        by_hour[h]["pv"].append(r["pv_kw"])
        by_hour[h]["price"].append(r["price"])
        by_hour[h]["temp"].append(r["temp_c"])
    hours = sorted(by_hour.keys())
    hour_curve = [{
        "hour": h,
        "load": round(sum(by_hour[h]["load"]) / len(by_hour[h]["load"]), 1),
        "pv": round(sum(by_hour[h]["pv"]) / len(by_hour[h]["pv"]), 1),
        "price": round(sum(by_hour[h]["price"]) / len(by_hour[h]["price"]), 4),
        "temp": round(sum(by_hour[h]["temp"]) / len(by_hour[h]["temp"]), 1),
    } for h in hours]
    # 按日
    daily = {}
    for r in query("SELECT ts, load_kw FROM hourly_records WHERE building=?", (b,)):
        d = r["ts"][:10]
        daily.setdefault(d, 0.0)
        daily[d] += r["load_kw"]
    days = sorted(daily.keys())
    daily_series = [{"date": d, "kwh": round(daily[d], 1)} for d in days]
    # 工作日 vs 周末
    wd = [r for r in df if r["is_workday"]]
    we = [r for r in df if not r["is_workday"]]
    wd_by_h = {}
    we_by_h = {}
    for r in wd:
        wd_by_h.setdefault(r["hour"], []).append(r["load_kw"])
    for r in we:
        we_by_h.setdefault(r["hour"], []).append(r["load_kw"])
    work_vs_weekend = [{
        "hour": h,
        "workday": round(sum(wd_by_h.get(h, [0])) / max(len(wd_by_h.get(h, [1])), 1), 1),
        "weekend": round(sum(we_by_h.get(h, [0])) / max(len(we_by_h.get(h, [1])), 1), 1),
    } for h in hours]
    return jsonify({"building": b, "hour_curve": hour_curve,
                    "daily_series": daily_series, "work_vs_weekend": work_vs_weekend})


# ---------------- 异常诊断 ----------------
@app.route("/api/anomaly")
def api_anomaly():
    try:
        return jsonify(anomaly.run())
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ---------------- 负荷预测 ----------------
@app.route("/api/forecast")
def api_forecast():
    try:
        r = forecast.evaluate()
        models = r.pop("models")
        future = forecast.predict_future(models, hours=24)
        return jsonify({
            "metrics": {"rmse": r["rmse"], "mae": r["mae"], "mape": r["mape"]},
            "per_building": r["per_building"],
            "n_train": r["n_train"], "n_test": r["n_test"],
            "future": future,
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ---------------- 储能调度 ----------------
@app.route("/api/storage")
def api_storage():
    try:
        days = int(request.args.get("days", 7))
        return jsonify(storage.run(days=days))
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ---------------- 碳核算 ----------------
@app.route("/api/carbon")
def api_carbon():
    try:
        r = carbon.report()
        # 转换日期为字符串（JSON 兼容）
        r["daily"] = [{"date": d["date"].strftime("%Y-%m-%d"),
                       "kwh": d["kwh"], "co2_t": d["co2_t"]} for d in r["daily"]]
        return jsonify(r)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ---------------- 系统设置：参数配置 ----------------
SETTINGS_PATH = os.path.join(BASE, "settings.json")

@app.route("/settings")
def page_settings():
    return render_template("settings.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """保存网页修改的配置（碳因子、电价、数据接入地址），写入 settings.json。"""
    try:
        new = request.get_json(force=True)
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            cur = json.load(f)
        # 只允许更新白名单字段
        for k in ("carbon_factor", "carbon_factor_source", "price_flat",
                  "data_api_url", "data_source"):
            if k in new:
                cur[k] = new[k]
        cur["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        # 热更新 config 模块
        config.CARBON_FACTOR = float(cur["carbon_factor"])
        config.CARBON_FACTOR_SOURCE = cur["carbon_factor_source"]
        config.PRICE_FLAT = float(cur["price_flat"])
        return jsonify({"ok": True, "saved": cur})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """一键重训异常检测模型（接入新数据后调用）。"""
    try:
        r = anomaly.run()
        res = r.get("residual", {})
        return jsonify({
            "ok": True,
            "f1": res.get("f1"),
            "precision": res.get("precision"),
            "recall": res.get("recall"),
            "tp": res.get("tp"), "fp": res.get("fp"), "fn": res.get("fn"),
            "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"慧碳安居已启动 → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
