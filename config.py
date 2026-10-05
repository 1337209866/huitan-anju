# -*- coding: utf-8 -*-
"""
慧碳安居 · 全局配置
所有参数均标注来源依据，便于申报书引用与答辩说明。
排放因子、电价等可在网页"系统设置"页修改，修改后写入 settings.json，本文件启动时加载覆盖默认值。
"""
import json, os

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
def _load_settings():
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
_settings = _load_settings()

# ============ 1. 碳排放因子（来源：生态环境部《关于发布2024年电力碳足迹因子数据的公告》） ============
# 2024年全国电力平均碳足迹因子 0.5777 kgCO2e/kWh（2023年为0.6205，同比下降6.9%）
CARBON_FACTOR = _settings.get("carbon_factor", 0.5777)
CARBON_FACTOR_SOURCE = _settings.get("carbon_factor_source", "生态环境部2024年电力碳足迹因子公告（全国平均 0.5777 kgCO2e/kWh）")

# ============ 2. 山东工商业分时电价（来源：国网山东电力《2026年工商业分时电价公告》） ============
# 浮动比例：尖峰上浮100%、高峰上浮70%、低谷下浮70%、深谷下浮90%（以平段为基准）
# 平段基准价取山东一般工商业综合电价水平约 0.70 元/kWh（示例基准，答辩可注明）
PRICE_FLAT = _settings.get("price_flat", 0.70)
PRICE_PEAK = round(PRICE_FLAT * 1.70, 4)    # 高峰 1.19
PRICE_SHARP = round(PRICE_FLAT * 2.00, 4)   # 尖峰 1.40
PRICE_VALLEY = round(PRICE_FLAT * 0.30, 4)  # 低谷 0.21
PRICE_DEEP = round(PRICE_FLAT * 0.10, 4)    # 深谷 0.07
PRICE_SOURCE = "国网山东电力2026年工商业分时电价公告（尖峰+100%/高峰+70%/低谷-70%/深谷-90%）"

# 2026年9-11月时段划分（来源：山东省发改委分时电价政策解读）
# 谷时段 9:00-15:00（含深谷11:00-14:00）；峰时段16:00-23:00（含尖峰17:00-22:00）；其余为平段
TOU_SCHEDULE = {
    "deep":  [(11, 14)],        # 深谷
    "valley": [(9, 11), (14, 15)],  # 低谷（除深谷外）
    "sharp": [(17, 22)],        # 尖峰
    "peak":  [(16, 17), (22, 23)],  # 高峰（除尖峰外）
}

# ============ 3. 建筑群参数（依据：广州2021年公共建筑电耗公示 + 北京DB11/T1413能耗标准） ============
# 参考值：写字楼 98 kWh/m²·a、国家机关办公 85.6、商场 149；住宅约30-40 kWh/m²·a
BUILDINGS = [
    {"id": "A", "name": "A栋 · 办公楼", "type": "办公", "area_m2": 10000,
     "annual_kwh_per_m2": 100, "base_kwh_day": 3560, "peak_power_kw": 320,
     "pv_capacity_kw": 400, "load_profile": "office"},
    {"id": "B", "name": "B栋 · 住宅楼", "type": "住宅", "area_m2": 8000,
     "annual_kwh_per_m2": 35, "base_kwh_day": 1000, "peak_power_kw": 150,
     "pv_capacity_kw": 100, "load_profile": "residential"},
    {"id": "C", "name": "C栋 · 商业楼", "type": "商业", "area_m2": 6000,
     "annual_kwh_per_m2": 150, "base_kwh_day": 2960, "peak_power_kw": 300,
     "pv_capacity_kw": 300, "load_profile": "commercial"},
]

# ============ 4. 光伏（园区屋顶BIPV，按楼栋屋顶配比） ============
PV_TOTAL_KW = 800             # 园区光伏总装机 800kWp
PV_SOURCE = "园区屋顶光伏合计800kWp（A栋400/B栋100/C栋300），年发电量按 装机×峰值日照×系统效率 估算（山东年峰值日照约1200-1400h）"

# 楼栋峰值功率速查（供异常检测特征归一化使用）
BUILDING_PEAK_KW = {b["id"]: b["peak_power_kw"] for b in BUILDINGS}

# ============ 5. 储能系统（园区配置） ============
STORAGE_CAPACITY_KWH = 500    # 储能容量 500kWh
STORAGE_POWER_KW = 200        # 最大充/放功率 200kW
STORAGE_EFF = 0.92            # 充放电综合效率（往返）
STORAGE_SOURCE = "园区储能 500kWh/200kW，往返效率92%（工程常见值）"

# ============ 6. 数据生成参数 ============
START_DATE = "2026-07-01"     # 90天模拟区间：2026-07-01 ~ 2026-09-28（夏季空调负荷显著）
N_DAYS = 90
SEED = 42
