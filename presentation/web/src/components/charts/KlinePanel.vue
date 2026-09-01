<!--
  KlinePanel K线形态回放（可视化重构 P2 · lightweight-charts 终于上战场 2026-09-01）。

  物理意图：把颈线法信号画回 K 线——candlestick + 定身位 priceLine（entry/止损/
  TP1/TP2/颈线）+ 信号日 marker（▲）。数据=ohlcv_<sym>.json 静态快照（160 根日K，
  前复权与湖同源）；marks 任一位缺失则该线不画（绝不用猜的价位画线）。
  A 股视觉惯例：阳线红 #ef5350 / 阴线绿 #26a69a（与 terminal-dark 主题同源）。
-->
<template>
  <div ref="el" class="kline"></div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  createChart, CandlestickSeries, createSeriesMarkers, ColorType,
  type IChartApi, type ISeriesApi, type CandlestickData, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import type { OhlcvData } from '../../api/home'

const props = defineProps<{ data: OhlcvData }>()
const el = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let series: ISeriesApi<'Candlestick'> | null = null

const LINE = {
  entry: { color: '#2962ff', title: 'entry' },
  stop: { color: '#26a69a', title: '止损' },
  tp1: { color: '#ef5350', title: 'TP1' },
  tp2: { color: '#f23645', title: 'TP2' },
  neck: { color: '#f0b90b', title: '颈线' },
}

function render() {
  if (!el.value || !props.data?.dates?.length) return
  if (!chart) {
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
      rightPriceScale: { borderColor: '#dcdfe6' },
      timeScale: { borderColor: '#dcdfe6' },
      localization: { locale: 'zh-CN' },
    })
    series = chart.addSeries(CandlestickSeries, {
      upColor: '#ef5350', downColor: '#26a69a',
      borderUpColor: '#ef5350', borderDownColor: '#26a69a',
      wickUpColor: '#ef5350', wickDownColor: '#26a69a',
    })
  }
  const rows = props.data.rows
  const kdata: CandlestickData<Time>[] = props.data.dates.map((d, i) => ({
    time: d as unknown as UTCTimestamp,
    open: rows[i][0], high: rows[i][1], low: rows[i][2], close: rows[i][3],
  }))
  series!.setData(kdata)

  const m = props.data.marks || {}
  const priceLines: Array<[number | null | undefined, typeof LINE[keyof typeof LINE], boolean]> = [
    [m.entry_price, LINE.entry, false], [m.stop, LINE.stop, false],
    [m.tp1_price, LINE.tp1, false], [m.tp2_price, LINE.tp2, false],
    [m.neckline, LINE.neck, true],
  ]
  for (const [price, style, dashed] of priceLines) {
    if (price != null && Number.isFinite(price) && series) {
      series.createPriceLine({
        price, color: style.color, lineWidth: 1,
        lineStyle: dashed ? 2 : 0, axisLabelVisible: true, title: style.title,
      })
    }
  }

  // 信号/进场 marker（同一日则只留信号▲）
  const markers: Array<{ time: Time; position: 'belowBar' | 'aboveBar'
    color: string; shape: string; text: string }> = []
  const formed = m.formed_at || m.entry_date
  if (formed && props.data.dates.includes(formed) && series) {
    markers.push({ time: formed as unknown as UTCTimestamp, position: 'belowBar',
      color: '#f0b90b', shape: 'arrowUp', text: '信号' })
  }
  if (m.entry_date && m.entry_date !== formed && props.data.dates.includes(m.entry_date) && series) {
    markers.push({ time: m.entry_date as unknown as UTCTimestamp, position: 'belowBar',
      color: '#2962ff', shape: 'circle', text: '进场' })
  }
  if (series) {
    createSeriesMarkers(series, markers as never)
  }
  chart.timeScale().fitContent()
}

onMounted(render)
watch(() => props.data, render)
onBeforeUnmount(() => {
  chart?.remove()
  chart = null
})
</script>

<style scoped>
.kline { height: 380px; width: 100%; }
</style>
