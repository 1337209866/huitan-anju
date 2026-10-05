/* ============================================================
   能碳管家 · 公共 JS
   提供：fetchJSON / 加载动画 / ECharts 渲染封装 / 日期工具
   ============================================================ */

/** 请求 JSON，失败时显示错误 */
async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const data = await resp.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

/** 页面加载提示（放在容器里） */
function showLoading(el) {
  if (!el) return;
  el.innerHTML =
    '<div class="loading"><div class="spinner"></div><span>数据加载中…</span></div>';
}

/** 渲染失败提示 */
function showError(el, msg) {
  if (!el) return;
  el.innerHTML = '<div class="error-box">数据加载失败：' + (msg || '未知错误') +
    '。请确认后端已启动（python app.py）。</div>';
}

/** 初始化 ECharts 实例（元素不存在返回 null） */
function initChart(el) {
  if (!el) return null;
  if (typeof echarts === 'undefined') {
    el.innerHTML = '<div class="error-box">ECharts 库加载失败</div>';
    return null;
  }
  const chart = echarts.init(el);
  window.addEventListener('resize', () => chart.resize());
  return chart;
}

/** 设置图表并自适应宽度 */
function renderChart(chart, option) {
  if (!chart) return;
  chart.setOption(option, true);
  setTimeout(() => chart.resize(), 50);
}

/** 千分位格式化 */
function fmtNum(v, digits) {
  if (v === null || v === undefined) return '--';
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits || 0,
    maximumFractionDigits: digits || 0,
  });
}

/** 日期格式化 YYYY-MM-DD */
function todayStr() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
    '-' + String(d.getDate()).padStart(2, '0');
}

/** 颜色池（柔和舒适） */
const PALETTE = ['#4a90d9', '#3eb575', '#f5a623', '#8b6fd8', '#36b8c9',
                 '#e85c5c', '#e1b98f', '#a3d5e8'];

/** 通用 tooltip 样式 */
const TOOLTIP = {
  trigger: 'axis',
  backgroundColor: 'rgba(255,255,255,0.96)',
  borderColor: '#e8ecf1',
  textStyle: { color: '#2b3245', fontSize: 12 },
  confine: true,
};
