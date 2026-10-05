# 慧碳安居 · 保障房园区能耗智能管理助手

面向保障房和产业园区的轻量级能耗管理平台，覆盖**异常诊断、负荷预测、储能调度、碳核算 ESG、智能对话助手**五大能力。浏览器访问，SQLite 存储，无需重型中间件，一台普通电脑即可运行。

- 参赛方向：④ 运营（保障房 / 产业园 · 能源管理 + ESG）
- 团队：朱睿（总体设计/异常检测/文档）、刘浩威（数据模拟/储能/前端/碳核算）
- 在线演示：**https://huitan-anju.loca.lt**（固定地址，重启电脑不变）

---

## 一、项目结构

```
energy-carbon-manager/
├── app.py                  # Flask 后端入口（页面路由 + REST API + 系统设置）
├── config.py               # 全局配置（碳因子/电价/楼栋/储能参数）
├── settings.json           # 网页可修改的配置（碳因子、电价、数据接入）
├── energy.db               # SQLite 数据库（90天×3栋×24小时 = 6480条逐时记录）
├── requirements.txt        # Python 依赖
├── agent/
│   └── assistant.py         # 智能对话助手（意图识别 + 工具调用 + 数据建议引擎）
├── algo/                   # 四大算法模块
│   ├── anomaly.py          # 异常检测：预测残差法 + 两阶段鲁棒训练 + CUSUM 双路
│   ├── forecast.py         # 负荷预测：逐楼栋 LightGBM
│   ├── storage.py          # 储能调度：scipy 线性规划（峰谷套利）
│   └── carbon.py            # 碳核算：排放因子法
├── generator/
│   ├── data_generator.py    # 数据生成器 v2（热惯性温度/AR(1)噪声/马尔可夫天气/三类隐蔽异常）
│   └── self_check.py        # 生成数据合理性自检
├── tools/
│   └── start_fixed.bat      # 一键启动：Flask + localtunnel 固定隧道
├── templates/               # 前端页面（总览/能耗/异常/预测/储能/碳核算/对话/系统设置）
└── static/                 # CSS/JS
```

## 二、环境准备

需要 Python 3.9+（开发环境为 Anaconda，Python 3.10），以及 Node.js 16+（仅公网访问需要，本地运行不需要）。

```bash
# 安装依赖（建议在虚拟环境或 conda 环境中执行）
pip install -r requirements.txt
```

依赖清单：Flask、LightGBM、scikit-learn、pandas、numpy、scipy。

## 三、启动步骤

### 方式一：本地运行（离线可用）

```bash
# 1. 启动 Flask 服务
python app.py
```

浏览器打开 http://127.0.0.1:5000 即可访问。终端显示 `慧碳安居已启动 → http://127.0.0.1:5000` 表示成功。

### 方式二：公网访问（固定 HTTPS 地址，供演示/评委在线体验）

一键启动（推荐）：

```bash
# Windows 双击即可，会同时拉起 Flask 和 localtunnel 隧道
tools/start_fixed.bat
```

手动方式：

```bash
# 1. 先启动本地服务
python app.py

# 2. 另开终端，启动 localtunnel 并指定固定子域名
npx -y localtunnel --port 5000 --subdomain huitan-anju
```

启动成功后，固定公网地址为 **https://huitan-anju.loca.lt**（子域名已写死，重启电脑后地址不变）。

> **首次访问提示页**：localtunnel 有一个一次性安全提示页（防滥用），页面上会显示一行 IP 地址，把那个 IP 填进输入框点 Continue 即可进入系统。每个访客的浏览器 7 天内只会看到一次。当前出口 IP 显示在提示页顶部，以页面显示为准。

> **注意**：公网访问期间需要保持演示电脑开机，且 `app.py` 和 localtunnel 进程在运行。电脑关机后隧道即失效，重新双击 `tools/start_fixed.bat` 即可恢复。

## 四、数据说明（可选）

系统默认自带 `energy.db`（90 天模拟数据，已按真实建筑能耗规律生成并埋入三类隐蔽异常），**无需额外操作即可启动**。

如需重新生成数据（修改了 config.py 中的楼栋/天气/异常参数后）：

```bash
# 重新生成数据并写入 energy.db
python generator/data_generator.py

# 自检数据合理性（负荷量级、异常占比/幅度、电价时段等指标）
python generator/self_check.py
```

数据口径摘要：办公楼 100 kWh/㎡·a、住宅 35、商业 150；三类异常为 A 栋空调 COP 渐变退化、C 栋部分周末未关空调、B 栋深夜基荷漂移；异常占比约 4.8%（参考真实园区 1-5%）。

## 五、常用功能与接口

| 页面 | 地址 | 说明 |
|---|---|---|
| 总览 | `/` | 总用电/光伏/碳排/峰值 + 30 天趋势 + 楼栋占比 |
| 能耗分析 | `/energy` | 楼栋 24h 负荷曲线、工作日 vs 周末 |
| 异常诊断 | `/anomaly` | 预测残差法双路检测 + 三类异常分型 |
| 负荷预测 | `/forecast` | LightGBM 未来 24h 预测 |
| 储能调度 | `/storage` | 线性规划充放电计划与节省金额 |
| 碳核算 | `/carbon` | 排放因子法 + 光伏贡献率 |
| 智能助手 | `/chat` | 自然语言问答，回答末尾自动给出数据建议 |
| 系统设置 | `/settings` | 碳因子/电价在线修改、数据接入配置、一键重训 |

主要 API：`/api/chat`（对话）、`/api/anomaly`、`/api/forecast`、`/api/storage`、`/api/carbon`、`/api/config`（读写配置）、`/api/retrain`（重训异常检测模型）。

## 六、接入真实数据

系统采用配置驱动架构，换园区/接真实电表无需改算法代码：

1. 在网页"系统设置"页选择数据源（MQTT / Modbus / HTTP），填写网关地址；
2. 数据格式：每小时一条记录，字段 `ts`(ISO8601)、`building`(A/B/C)、`load_kw`、`pv_kw`、`outdoor_temp`；
3. 数据写入 SQLite 后，在"系统设置"页点"一键重训"，异常检测模型用真实数据重新拟合；
4. 碳因子/电价变化时，直接在"系统设置"页修改，碳核算与储能调度即时按新参数重算（保存于 `settings.json`）。

## 七、常见问题

- **启动报错 ModuleNotFoundError**：确认已执行 `pip install -r requirements.txt`，且用同一个 Python 环境运行。
- **端口被占用**：改 `app.py` 最后一行 `port=5000` 为其他端口，或在启动前关闭占用 5000 端口的进程。
- **打开 https://huitan-anju.loca.lt 提示 "You are about to visit"**：这是 localtunnel 的一次性安全提示页，把页面上方"This tunnel is hosted by"后面显示的 IP 填进输入框，点 Continue 即可。
- **公网地址打不开**：确认 `tools/start_fixed.bat` 已运行，Flask 和 localtunnel 进程都在；电脑关机后隧道会失效，重新双击脚本即可。
- **异常检测 F1 为什么不是 100%**：真实园区异常与噪声同量级，F1=0.335 是刻意贴近真实的结果；模型页面同时展示与统计基线的对比。
