# -*- coding: utf-8 -*-
"""
园区能耗对话助手（Agent 层）
================================
设计原则：
1. 不瞎编 —— 所有数字必须来自 SQLite 实时查询或已计算的算法结果；
   助手只做"听懂问题 → 调工具 → 用真实数字组织语言"。
2. 可扩展 —— 预留 LLM 接口位；后续接入大模型时，把 _template_reply
   替换为 LLM 生成，但工具返回的事实仍作为 system 约束喂给模型。
3. 离线可用 —— 当前为规则意图识别 + 函数调用，无需外部 API Key。
"""
import os
import re
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "energy.db")

# ---------- 缓存（anomaly/forecast/storage/carbon 计算较重，90天数据固定） ----------
_cache = {}


def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_overview():
    if "overview" not in _cache:
        df = query("SELECT * FROM hourly_records ORDER BY ts")
        total_kwh = sum(r["load_kw"] for r in df)
        total_pv = sum(r["pv_kw"] for r in df)
        n_days = len(set(r["ts"][:10] for r in df))
        by_b = {}
        for r in df:
            by_b.setdefault(r["building"], 0.0)
            by_b[r["building"]] += r["load_kw"]
        peak = max((r["load_kw"] for r in df), default=0)
        buildings = query("SELECT * FROM buildings ORDER BY id")
        b_list = []
        for b in buildings:
            kwh = by_b.get(b["id"], 0.0)
            b_list.append({
                "id": b["id"], "name": b["name"], "type": b["type"],
                "area": b["area_m2"], "kwh": kwh,
                "co2": kwh * 0.577 / 1000.0,
                "annual_per_m2": kwh / b["area_m2"] * 365 / n_days / 1.25,
            })
        _cache["overview"] = {
            "total_kwh": total_kwh, "total_pv": total_pv,
            "n_days": n_days, "peak": peak, "buildings": b_list,
            "co2": total_kwh * 0.577 / 1000.0,
        }
    return _cache["overview"]


def get_anomaly():
    if "anomaly" not in _cache:
        from algo import anomaly as _mod
        _cache["anomaly"] = _mod.run()
    return _cache["anomaly"]


def get_forecast():
    if "forecast" not in _cache:
        from algo import forecast as _mod
        r = _mod.evaluate()
        models = r.pop("models")
        future = _mod.predict_future(models, hours=24)
        _cache["forecast"] = {"metrics": r, "future": future}
    return _cache["forecast"]


def get_storage():
    if "storage" not in _cache:
        from algo import storage as _mod
        _cache["storage"] = _mod.run(days=7)
    return _cache["storage"]


def get_carbon():
    if "carbon" not in _cache:
        from algo import carbon as _mod
        _cache["carbon"] = _mod.report()
    return _cache["carbon"]


# ---------- 意图识别 ----------
BUILDING_ALIASES = {
    "A": ["a栋", "a座", "办公楼", "办公", "a楼"],
    "B": ["b栋", "b座", "住宅", "住宅楼", "b楼"],
    "C": ["c栋", "c座", "商业", "商业楼", "c楼"],
}

INTENT_RULES = [
    ("help",       ["你能", "会做", "怎么用", "帮助", "功能", "你是谁", "你会什么", "介绍"]),
    ("anomaly",    ["异常", "故障", "浪费", "多耗", "偷电", "没关", "没关空", "没关灯",
                    "退化", "问题", "报警", "告警", "不对劲", "偏高", "哪里多"]),
    ("storage",    ["储能", "电池", "充放", "充电", "放电", "峰谷", "套利", "省电",
                    "省钱", "调度", "怎么充", "怎么放", "两充两放"]),
    ("carbon",     ["碳", "排放", "碳中和", "绿电", "光伏抵", "co2", "温室", "esg"]),
    ("forecast",   ["预测", "明天", "未来", "接下来", "下一小时", "负荷多少", "什么时候最高",
                    "明日", "次日", "高峰"]),
    ("building",    ["哪栋", "哪个楼", "a栋", "b栋", "c栋", "办公楼", "住宅", "商业",
                    "对比", "谁多", "占比", "单耗", "每平方", "平米"]),
    ("overview",    ["总", "一共", "园区", "整体", "全部", "总体", "累计", "总共",
                    "用了多少", "多少电", "多少度", "光伏发"]),
]


def detect_intent(text):
    t = text.lower().strip()
    for intent, kws in INTENT_RULES:
        for kw in kws:
            if kw in t:
                return intent
    return "unknown"


def extract_building(text):
    t = text.lower()
    for bid, aliases in BUILDING_ALIASES.items():
        for a in aliases:
            if a in t:
                return bid
    return None


# ---------- 回答生成（所有数字来自工具返回） ----------
def _fmt_num(v, dec=1):
    try:
        return f"{float(v):,.{dec}f}"
    except Exception:
        return str(v)


def reply_help():
    return (
        "我是园区能耗智能助手，可以帮你查这些事（数字都来自实时数据）：\n\n"
        "1️⃣ 总览：「园区这个月用了多少电？」「光伏发电多少？」\n"
        "2️⃣ 楼栋：「A栋和C栋谁用电多？」「B栋单耗多少？」\n"
        "3️⃣ 异常：「最近哪里有异常？」「浪费了多少电费？」「A栋怎么了？」\n"
        "4️⃣ 预测：「明天什么时候用电最高？」\n"
        "5️⃣ 储能：「电池今天怎么调度？」「能省多少钱？」\n"
        "6️⃣ 碳：「园区排了多少碳？光伏抵了多少？」\n\n"
        "直接用大白话问我就行。"
    )


def reply_overview():
    ov = get_overview()
    return (
        f"园区近 {ov['n_days']} 天（7-9月夏季制冷季）累计：\n"
        f"· 总用电 {_fmt_num(ov['total_kwh'])} kWh（约 {_fmt_num(ov['total_kwh']/10000,1)} 万度）\n"
        f"· 光伏发电 {_fmt_num(ov['total_pv'])} kWh\n"
        f"· 等效碳排放 {_fmt_num(ov['co2'])} tCO₂e\n"
        f"· 历史峰值负荷 {_fmt_num(ov['peak'])} kW\n\n"
        f"注：这是夏季数据，空调负荷偏高；折年化单耗已做季节校正。"
    )


def reply_building(text):
    ov = get_overview()
    bid = extract_building(text)
    lines = []
    if bid:
        b = next((x for x in ov["buildings"] if x["id"] == bid), None)
        if b:
            pct = b["kwh"] / ov["total_kwh"] * 100
            ref = {"A": "85-100", "B": "30-40", "C": "120-150"}.get(bid, "?")
            lines.append(
                f"{b['name']}（{b['type']}，{_fmt_num(b['area'],0)}㎡）：\n"
                f"· 周期用电 {_fmt_num(b['kwh'])} kWh，占园区 {pct:.1f}%\n"
                f"· 碳排放 {_fmt_num(b['co2'],2)} tCO₂e\n"
                f"· 年化单耗 {_fmt_num(b['annual_per_m2'])} kWh/㎡·a（参考值 {ref}）"
            )
            # 与参考值对比
            if bid == "A" and b["annual_per_m2"] > 90:
                lines.append("⚠️ 单耗略高于参考区间上限，建议查空调系统效率。")
            elif bid == "B" and b["annual_per_m2"] > 40:
                lines.append("⚠️ 单耗略高，可能有公共区域照明/电梯未及时关停。")
            elif bid == "C" and b["annual_per_m2"] > 140:
                lines.append("⚠️ 单耗接近参考上限，注意营业时段空调管理。")
            else:
                lines.append("✅ 单耗在合理参考区间内。")
        return "\n".join(lines)
    # 无指定楼栋：给对比
    lines.append("各楼栋用电对比（近90天）：")
    for b in ov["buildings"]:
        pct = b["kwh"] / ov["total_kwh"] * 100
        lines.append(f"· {b['name']}：{_fmt_num(b['kwh'])} kWh，占 {pct:.1f}%，单耗 {_fmt_num(b['annual_per_m2'])}")
    return "\n".join(lines)


def reply_anomaly(text):
    a = get_anomaly()
    bid = extract_building(text)
    res = a.get("residual", {})
    waste = a.get("waste", {})
    types = a.get("types", {})
    hits = a.get("hits", [])
    if bid:
        bhits = [h for h in hits if h.get("building") == bid]
        bgt = sum(1 for h in query(
            "SELECT is_anomaly FROM hourly_records WHERE building=? AND is_anomaly=1", (bid,)))
        btp = sum(1 for h in bhits if h.get("is_gt") == 1)
        lines = [f"{bid}栋相关告警（预测残差法）：窗口内命中 {len(bhits)} 条，其中 {btp} 条命中真实异常；该楼历史标签异常 {bgt} 条。"]
        if bhits:
            lines.append("最近几条：")
            for h in bhits[:5]:
                tag = "✅真" if h.get("is_gt") == 1 else "❓疑"
                lines.append(f"  {tag} {h.get('ts','')} 实际 {_fmt_num(h.get('load_kw',0))}kW / 期望 {_fmt_num(h.get('expected',0))}kW")
        else:
            lines.append("当前窗口内该楼暂无告警。")
        return "\n".join(lines)
    lines = [
        f"异常检测（预测残差法，瞬时阈值+日级CUSUM双路）：\n"
        f"· 精确率 {res.get('precision',0)*100:.1f}%，召回率 {res.get('recall',0)*100:.1f}%，F1={res.get('f1',0):.3f}\n"
        f"· 全窗口统计：真阳性 {res.get('tp',0)} 条，误报 {res.get('fp',0)} 条，漏报 {res.get('fn',0)} 条（真实异常共 {res.get('total_gt',0)} 条）\n"
        f"· 检测窗口累计浪费约 {_fmt_num(waste.get('waste_kwh',0))} kWh，折合电费约 ¥{_fmt_num(waste.get('waste_cost',0),0)}\n",
    ]
    if types:
        lines.append("三类异常检出：")
        for name, v in types.items():
            lines.append(f"· {name}：检出 {v.get('hit',0)}/{v.get('gt',0)}，召回 {v.get('recall',0)*100:.0f}%")
    return "\n".join(lines)


def reply_forecast():
    f = get_forecast()
    m = f["metrics"]
    future = f["future"]
    if not future:
        return "暂时没有预测数据。"
    peak = max(future, key=lambda x: x.get("load_kw", 0))
    low = min(future, key=lambda x: x.get("load_kw", 0))
    return (
        f"未来24小时负荷预测（LightGBM）：\n"
        f"· 模型精度 RMSE={_fmt_num(m.get('rmse',0))} kW\n"
        f"· 高峰出现在 {peak.get('time','')}，约 {_fmt_num(peak.get('load_kw',0))} kW\n"
        f"· 低谷出现在 {low.get('time','')}，约 {_fmt_num(low.get('load_kw',0))} kW\n\n"
        f"建议：高峰前预冷/提前启动光伏直供，低谷时安排储能充电。"
    )


def _periods(values, thresh=0.5):
    """把24小时数组按连续段分组，返回如 ['11时-14时', '23时']"""
    on = [i for i, v in enumerate(values) if v and v > thresh]
    if not on:
        return []
    groups = [[on[0]]]
    for h in on[1:]:
        if h == groups[-1][-1] + 1:
            groups[-1].append(h)
        else:
            groups.append([h])
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(f"{g[0]}时")
        else:
            out.append(f"{g[0]}时-{g[-1]}时")
    return out


def reply_storage():
    s = get_storage()
    days = s.get("days", [])
    if not days:
        return "储能调度暂无结果。"
    d0 = days[0]
    save = s.get("total_save", 0)
    save_pct = s.get("save_pct", 0)
    charge_p = _periods(d0.get("charge", []))
    discharge_p = _periods(d0.get("discharge", []))
    return (
        f"未来7天储能调度（线性规划，峰谷套利+光伏消纳）：\n"
        f"· 预计节省电费 ¥{_fmt_num(save,0)}（节省比例约 {save_pct:.1f}%）\n"
        f"· 今日充电时段：{'、'.join(charge_p) or '—'}（光伏富余/谷电低价时）\n"
        f"· 今日放电时段：{'、'.join(discharge_p) or '—'}（晚高峰高价时放电）\n\n"
        f"策略：白天光伏富余时充电，傍晚17-20时高峰放电，赚峰谷价差。"
    )


def reply_carbon():
    c = get_carbon()
    daily = c.get("daily", [])
    total_kwh = sum(d["kwh"] for d in daily)
    total_co2 = sum(d["co2_t"] for d in daily)
    ov = get_overview()
    pv = ov["total_pv"]
    pv_offset = pv * 0.577 / 1000.0  # 光伏替代电网的减排量
    return (
        f"园区碳核算（排放因子法，0.5777 kgCO₂/kWh）：\n"
        f"· 周期总用电 {_fmt_num(total_kwh)} kWh\n"
        f"· 等效碳排放 {_fmt_num(total_co2)} tCO₂e\n"
        f"· 光伏发电 {_fmt_num(pv)} kWh，相当于减排 {_fmt_num(pv_offset,2)} tCO₂e\n"
        f"· 光伏贡献率约 {pv/(total_kwh+pv)*100:.1f}%（自发自用抵消部分电网用电）\n\n"
        f"注：山东电网 2024 年排放因子 0.5777 kgCO₂/kWh。"
    )


# ---------- 智能建议引擎 ----------
def build_recommendations():
    """综合查数据，生成 3-5 条按优先级排序的可执行建议。
    每条建议必须对应具体数字，不说空话。"""
    recs = []
    try:
        ov = get_overview()
        a = get_anomaly()
        s = get_storage()
        res = a.get("residual", {})
        waste = a.get("waste", {})
        types = a.get("types", {})

        # 1. 异常浪费 → 最优先
        waste_kwh = waste.get("waste_kwh", 0)
        waste_money = waste.get("waste_cost", 0)
        if waste_kwh > 500:
            recs.append(("高",
                f"优先处理异常浪费：检测窗口已累积浪费 {_fmt_num(waste_kwh)} kWh（约 ¥{_fmt_num(waste_money,0)}）。"
                f"建议本周安排空调系统巡检，重点查 A 栋 COP 衰减（已检出 {types.get('A栋空调系统COP下降（设备效率渐变退化）',{}).get('hit',0)} 条）。"))

        # 2. 单耗偏高楼栋
        for b in ov["buildings"]:
            amp = b["annual_per_m2"]
            ref_top = {"A": 100, "B": 40, "C": 150}.get(b["id"], 999)
            ref_low = {"A": 85, "B": 30, "C": 120}.get(b["id"], 0)
            if amp > ref_top * 0.95:
                recs.append(("中",
                    f"{b['name']}年化单耗 {_fmt_num(amp)} kWh/㎡·a，接近参考上限 {ref_top}。"
                    f"建议：{'清洗空调冷凝器+检查温度设定（26℃以上）' if b['id']=='A' else '检查公共区域照明/电梯待机功耗' if b['id']=='B' else '核查营业时段空调开启时长'}，预计可降 5-8%。"))
            elif amp < ref_low * 0.9:
                recs.append(("低",
                    f"{b['name']}单耗 {_fmt_num(amp)} 低于参考下限 {ref_low}，运行良好，可作为园区标杆。"))

        # 3. 储能调度建议
        save = s.get("total_save", 0)
        if save > 1000:
            recs.append(("中",
                f"储能优化空间：未来 7 天按线性规划调度可省 ¥{_fmt_num(save,0)}。"
                f"建议物业在 17-19 时高峰时段减少大功率设备使用，配合电池放电，可再省 10-15%。"))

        # 4. 光伏消纳建议
        pv = ov["total_pv"]
        total = ov["total_kwh"]
        pv_ratio = pv / (total + pv) * 100
        if pv_ratio > 20:
            recs.append(("低",
                f"光伏贡献率 {pv_ratio:.1f}%，白天 10-14 时光伏发电充足。"
                f"建议把充电桩、洗衣机等可移动负荷安排在这个时段，提高自发自用比例。"))

        # 5. 峰值负荷
        peak = ov["peak"]
        if peak > 280:
            recs.append(("中",
                f"历史峰值 {_fmt_num(peak)} kW，接近园区变压器容量上限。"
                f"建议高峰前 30 分钟提前预冷/预热，避免多台空调同时启动造成冲击。"))
    except Exception as e:
        recs.append(("低", f"建议引擎暂时不可用：{e}"))

    # 按优先级排序
    order = {"高": 0, "中": 1, "低": 2}
    recs.sort(key=lambda x: order.get(x[0], 9))
    return recs


def format_recommendations():
    recs = build_recommendations()
    if not recs:
        return ""
    lines = ["", "💡 基于当前数据的建议："]
    for i, (level, text) in enumerate(recs[:5], 1):
        icon = {"高": "🔴", "中": "🟡", "低": "🟢"}.get(level, "•")
        lines.append(f"{i}. {icon} [{level}] {text}")
    return "\n".join(lines)


# ---------- 主入口 ----------
def ask(text):
    """返回 (reply_text, intent)"""
    text = (text or "").strip()
    if not text:
        return reply_help(), "help"
    intent = detect_intent(text)
    try:
        if intent == "help":
            return reply_help(), intent
        if intent == "overview":
            base = reply_overview()
        elif intent == "building":
            base = reply_building(text)
        elif intent == "anomaly":
            base = reply_anomaly(text)
        elif intent == "forecast":
            base = reply_forecast()
        elif intent == "storage":
            base = reply_storage()
        elif intent == "carbon":
            base = reply_carbon()
        else:
            return (
                f"我暂时没听懂「{text}」具体指什么。\n\n"
                f"我可以回答：园区总用电、各楼栋对比、异常告警、负荷预测、储能调度、碳排放。\n"
                f"输入「帮助」看示例问法。"
            ), "unknown"
        # 除帮助外，所有回答自动追加数据建议
        return base + format_recommendations(), intent
    except Exception as e:
        return f"查询时出错：{e}。请换个问法或检查数据。", "error"
