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
    <div ref="el" class="kline"></div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  createChart, CandlestickSeries, HistogramSeries, createSeriesMarkers,
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
  const lines: Array<[number | null | undefined, string, string, boolean]> = [
    [m.entry_price, '#2962ff', 'entry', false],
    [m.stop, '#26a69a', '止损', false],
    [m.tp1_price, '#ef5350', 'TP1', false],
    [m.tp2_price, '#f23645', 'TP2', false],
    [m.neckline, '#f0b90b', '颈线', true],
  ]
  for (const [price, color, title, dashed] of lines) {
    if (price != null && Number.isFinite(price)) {
      priceLines.push(candle.createPriceLine({
        price, color, lineWidth: 1, lineStyle: dashed ? 2 : 0,
        axisLabelVisible: true, title,
      }))
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
  })

  applyRange()
}

function applyRange() {
  if (!chart || !props.data?.dates?.length) return
  const n = props.data.dates.length
  if (range.value === 'all' || n <= Number(range.value)) {
    chart.timeScale().fitContent()
    return
  }
  const start = props.data.dates[n - Number(range.value)]
  const end = props.data.dates[n - 1]
  chart.timeScale().setVisibleRange(
    { from: start as unknown as Time, to: end as unknown as Time })
}

onMounted(render)
watch(() => props.data, render)
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
.kline { height: 380px; width: 100%; }
</style>
