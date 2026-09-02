<!--
  KlinePanel K线形态回放（lightweight-charts v5 · 2026-09-01 交互升级）。

  物理意图：把颈线法信号画回 K 线——candlestick+成交量副图 + 定身位 priceLine
  （entry/止损/TP1/TP2/颈线）+ 信号 marker。交互显式开启（滚轮缩放/拖拽平移/
  双指捏合——抽屉场景手势不再被默认吞掉）；悬停十字光标联动顶部 OHLC 读数；
  区间按钮 60/120/全部（可见范围一键切换，移动端友好）。
  A 股惯例：阳线红 #ef5350 / 阴线绿 #26a69a；缺 marks 的位不画线（不猜价）。
-->
<template>
  <div class="kline-wrap">
    <div class="kline-toolbar">
      <div class="ohlc-legend">
        <span class="sym">{{ data.symbol }}</span>
        <span v-if="legend">开 {{ legend.o }} 高 {{ legend.h }} 低 {{ legend.l }}
          收 <b :class="legend.cls">{{ legend.c }}</b> 量 {{ legend.v }}</span>
        <span v-else class="hint">悬停看 OHLC · 滚轮缩放 · 拖拽平移</span>
      </div>
      <el-segmented v-model="range" :options="rangeOptions" size="small"
                    @change="applyRange" />
    </div>
    <div class="ma-row">
      <span v-for="ma in MA_DEFS" :key="ma.p" class="ma-chip"
            
            :style="maOn[ma.p] ? { color: ma.color, borderColor: ma.color } : {}"
            @click="toggleMa(ma.p)">
        MA{{ ma.p }}<em v-if="maVals[ma.p] != null"> {{ maVals[ma.p] }}</em>
      </span>
    </div>
    <div ref="el" class="kline"></div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  createChart, CandlestickSeries, HistogramSeries, LineSeries, createSeriesMarkers,
  ColorType, CrosshairMode,
  type IChartApi, type ISeriesApi, type IPriceLine,
  type CandlestickData, type HistogramData, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import type { OhlcvData } from '../../api/home'

const props = defineProps<{ data: OhlcvData }>()
const el = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let candle: ISeriesApi<'Candlestick'> | null = null
let volume: ISeriesApi<'Histogram'> | null = null
let priceLines: IPriceLine[] = []

const range = ref<'60' | '120' | 'all'>('120')
const rangeOptions = [
  { label: '60日', value: '60' },
  { label: '120日', value: '120' },
  { label: '全部', value: 'all' },
]

/** 均线组（09-01 用户需求②"默认展示 120 日线"）：MA5/10/20/60/120，
 *  默认开 20/60/120（5/10 关——降噪）；chip 可点切换；悬停读值。 */
const MA_DEFS = [
  { p: 5, color: '#f78166', on: false },
  { p: 10, color: '#d29922', on: false },
  { p: 20, color: '#2962ff', on: true },
  { p: 60, color: '#bc8cff', on: true },
  { p: 120, color: '#86909c', on: true },
] as const
const maOn = reactive<Record<number, boolean>>(
  Object.fromEntries(MA_DEFS.map((m) => [m.p, m.on])))
const maVals = ref<Record<number, string | null>>({})
let maSeries: Record<number, ISeriesApi<'Line'> | null> = {}

/** 前端算 MA（数据只有 160 根，13000 点内全数组算零成本）。 */
function maAt(closes: number[], period: number, i: number): number | null {
  if (i + 1 < period) return null
  let sum = 0
  for (let k = i + 1 - period; k <= i; k++) sum += closes[k]
  return +(sum / period).toFixed(2)
}

function rebuildMa() {
  if (!chart) return
  for (const k of Object.keys(maSeries)) {
    const p = Number(k)
    if (!maOn[p] && maSeries[p]) { chart.removeSeries(maSeries[p]!); maSeries[p] = null }
  }
  const closes = props.data.rows.map((r) => r[3])
  for (const def of MA_DEFS) {
    if (!maOn[def.p]) continue
    if (!maSeries[def.p]) {
      maSeries[def.p] = chart.addSeries(LineSeries, {
        color: def.color, lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false,
      })
    }
    maSeries[def.p]!.setData(props.data.dates.map((d, i) => ({
      time: d as unknown as UTCTimestamp,
      value: maAt(closes, def.p, i),
    })).filter((x) => x.value != null) as { time: Time; value: number }[])
  }
}

function toggleMa(p: number) {
  maOn[p] = !maOn[p]
  rebuildMa()
}

/** 悬停 OHLC 读数（十字光标联动）。 */
const legend = ref<{ o: string; h: string; l: string; c: string; v: string; cls: string } | null>(null)

const fmtP = (v: number) => v.toFixed(2)
const fmtV = (v: number) => (v >= 1e8 ? `${(v / 1e8).toFixed(2)}亿`
  : v >= 1e4 ? `${(v / 1e4).toFixed(1)}万` : String(v))

function destroyChart() {
  chart?.remove()
  chart = null
  candle = null
  volume = null
  priceLines = []
  maSeries = {}
}

function render() {
  if (!el.value || !props.data?.dates?.length) return
  destroyChart()
  chart = createChart(el.value, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: '#4e5969',
    },
    grid: {
      vertLines: { color: 'rgba(220,223,230,0.7)' },
      horzLines: { color: 'rgba(220,223,230,0.7)' },
    },
    rightPriceScale: { borderColor: '#dcdfe6', scaleMargins: { top: 0.06, bottom: 0.26 } },
    timeScale: { borderColor: '#dcdfe6', rightOffset: 4, barSpacing: 7 },
    crosshair: { mode: CrosshairMode.Normal },
    // 显式开启全部交互（滚轮/捏合/拖拽平移/轴拖缩放——抽屉与触屏场景不被吞）
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    handleScroll: { mouseWheel: true, pressedMouseMove: true,
                    horzTouchDrag: true, vertTouchDrag: true },
    localization: { locale: 'zh-CN' },
  })
  candle = chart.addSeries(CandlestickSeries, {
    upColor: '#ef5350', downColor: '#26a69a',
    borderUpColor: '#ef5350', borderDownColor: '#26a69a',
    wickUpColor: '#ef5350', wickDownColor: '#26a69a',
  })
  volume = chart.addSeries(HistogramSeries, {
    priceScaleId: 'volume', priceFormat: { type: 'volume' },
    lastValueVisible: false, priceLineVisible: false,
  })
  chart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.8, bottom: 0 }, borderVisible: false,
  })

  const rows = props.data.rows
  candle.setData(rows.map((r, i) => ({
    time: props.data.dates[i] as unknown as UTCTimestamp,
    open: r[0], high: r[1], low: r[2], close: r[3],
  })) as CandlestickData<Time>[])
  // 成交量柱：涨红跌绿（与 K 线同色语义）
  volume.setData(rows.map((r, i) => ({
    time: props.data.dates[i] as unknown as UTCTimestamp,
    value: r[4],
    color: r[3] >= r[0] ? 'rgba(239,83,80,0.45)' : 'rgba(38,166,154,0.45)',
  })) as HistogramData<Time>[])

  // 定身位画线（重画前清理——watch 重入防累积）
  for (const pl of priceLines) candle.removePriceLine(pl)
  priceLines = []
  const m = props.data.marks || {}
  // 决策线全实线+加粗（2026-09-02 用户反馈：虚线白底看不清）；axisLabel
  // 跟随线色（title 字号由库控制在 label 内）。
  // entry 语义分家（09-02 用户问"颈线和 entry 为什么基本一样"）：理论委托
  // =颈线+2.5×ATR（蓝实线，常远高于市价/被钳涨停）；成交成本=state 成交价
  //（青线，marketable limit 贴盘口≈颈线）。缺理论值时回落成交价（标 *）。
  const theoEntry = m.signal_entry ?? m.entry_price
  const lines: Array<[number | null | undefined, string, string]> = [
    [theoEntry, '#2962ff', m.signal_entry != null ? 'entry' : 'entry*'],
    [m.entry_price, '#13c2c2', '成本'],
    [m.stop, '#26a69a', '止损'],
    [m.tp1_price, '#ef5350', 'TP1'],
    [m.tp2_price, '#f23645', 'TP2'],
    [m.neckline, '#f0b90b', '颈线'],
  ]
  for (const [price, color, title] of lines) {
    if (price != null && Number.isFinite(price)) {
      priceLines.push(candle.createPriceLine({
        price, color, lineWidth: 2, lineStyle: 0,
        axisLabelVisible: true, title,
        axisLabelView: { color, fontSize: 11 },
      } as never))
    }
  }

  // 信号/进场 marker
  const markers: Array<{ time: Time; position: 'belowBar'; color: string
    shape: string; text: string }> = []
  const formed = m.formed_at || m.entry_date
  if (formed && props.data.dates.includes(formed)) {
    markers.push({ time: formed as unknown as UTCTimestamp, position: 'belowBar',
      color: '#f0b90b', shape: 'arrowUp', text: '信号' })
  }
  if (m.entry_date && m.entry_date !== formed && props.data.dates.includes(m.entry_date)) {
    markers.push({ time: m.entry_date as unknown as UTCTimestamp, position: 'belowBar',
      color: '#2962ff', shape: 'circle', text: '进场' })
  }
  createSeriesMarkers(candle, markers as never)

  // 十字光标联动 OHLC 读数
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !candle) { legend.value = null; return }
    const i = props.data.dates.indexOf(String(param.time))
    if (i < 0) { legend.value = null; return }
    const r = rows[i]
    legend.value = { o: fmtP(r[0]), h: fmtP(r[1]), l: fmtP(r[2]), c: fmtP(r[3]),
      v: fmtV(r[4]), cls: r[3] >= r[0] ? 'up' : 'down' }
    const closes = props.data.rows.map((x) => x[3])
    const vals: Record<number, string | null> = {}
    for (const def of MA_DEFS) {
      const v = maAt(closes, def.p, i)
      vals[def.p] = v != null ? fmtP(v) : null
    }
    maVals.value = vals
  })

  rebuildMa()
  applyRange()
}

function applyRange() {
  if (!chart || !props.data?.dates?.length) return
  const n = props.data.dates.length
  if (range.value === 'all' || n <= Number(range.value)) {
    chart.timeScale().fitContent()
    return
  }
  // 按序号钉窗口：首根 bar 索引 = n-N，右垫 0——可视范围严格=最近 N 根，
  // 左侧历史数据不进初始视口（09-02 用户截图 bug：autoSize 未就绪时
  // setVisibleRange 被布局重算覆盖 → 160 根全塞入+画线横穿全图）。
  const barCount = Number(range.value)
  chart.timeScale().applyOptions({ rightOffset: 0, barSpacing: 7 })
  chart.timeScale().setVisibleLogicalRange({
    from: n - barCount, to: n - 1,
  })
}

// 抽屉场景：el-dialog/drawer 开启动画期间容器宽度是中间态，autoSize 量到的
// 首帧会歪。rAF×2 等布局稳定再渲染；宽度仍未就绪（0）再等一轮。
function renderWhenReady(attempt = 0) {
  if (el.value && el.value.clientWidth > 50) {
    render()
    return
  }
  if (attempt > 20) { render(); return }        // 兜底：极端隐藏场景也强制画
  requestAnimationFrame(() => renderWhenReady(attempt + 1))
}

onMounted(() => renderWhenReady())
watch(() => props.data, () => renderWhenReady())
onBeforeUnmount(destroyChart)
</script>

<style scoped>
.kline-wrap { width: 100%; }
.kline-toolbar { display: flex; align-items: center; justify-content: space-between;
                 gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }
.ohlc-legend { display: flex; gap: 10px; font-size: 12px; color: #4e5969;
               align-items: baseline; flex-wrap: wrap; }
.ohlc-legend .sym { font-family: var(--qt-font-mono, monospace); font-weight: 600; }
.ohlc-legend .hint { color: var(--el-text-color-secondary); }
.up { color: #ef5350; }
.down { color: #26a69a; }
.ma-row { display: flex; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.ma-chip { font-size: 11px; padding: 1px 8px; border: 1px solid var(--qt-border, #dcdfe6);
           border-radius: 10px; color: var(--el-text-color-secondary); cursor: pointer;
           user-select: none; }
.ma-chip em { font-style: normal; font-family: var(--qt-font-mono, monospace); }
.kline { height: 380px; width: 100%; }
</style>
