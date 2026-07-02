<!--
  净值曲线 + 回撤图表组件

  职责：
  1. 使用 ECharts 渲染双 Y 轴图
     - 左轴：净值曲线（面积图，Quant 蓝）
     - 右轴：回撤深度（反向填充图，红）
  2. 响应式：窗口 resize 自动适配
  3. 组合模式额外渲染权重堆叠面积图

  设计原则：
  - 使用 vue-echarts 简化 ECharts 集成
  - 不引入 ECharts 全量包，仅按需引入折线图/面积图组件
  - 纯展示组件，无交互逻辑
  - 挂 terminal-dark 主题：轴/网格/tooltip 走主题，series 内联色仅作语义强调
-->
<template>
  <div class="chart-container">
    <!-- 净值曲线 + 回撤图 -->
    <div class="chart-section">
      <h3 class="chart-title">净值曲线与最大回撤</h3>
      <v-chart class="chart" :option="navChartOption" theme="terminal-dark" autoresize />
    </div>

    <!-- 权重堆叠面积图（仅组合模式显示） -->
    <div v-if="weightSeries && weightSeries.length > 0" class="chart-section">
      <h3 class="chart-title">资产权重时序</h3>
      <v-chart class="chart" :option="weightChartOption" theme="terminal-dark" autoresize />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
  ToolboxComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import type { NavPoint, DrawdownPoint, WeightPoint } from '../api/backtest'

// 按需注册 ECharts 组件（避免全量引入）
use([
  LineChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
  ToolboxComponent,
  CanvasRenderer,
])

const props = defineProps<{
  navSeries: NavPoint[]
  drawdownSeries: DrawdownPoint[]
  weightSeries?: WeightPoint[]
}>()

/**
 * 净值曲线 + 回撤图配置
 *
 * markRaw 隔离：option 含数百点数值数组 + tooltip 闭包，
 * 用 markRaw 阻止 Vue 对其做深度响应式代理。
 * 否则 Vue 会递归代理整棵配置树（含 echarts 内部引用），
 * 既浪费内存又会污染 echarts.setOption 的纯对象契约。
 * 数据本体已由父组件 shallowRef 持有（只读），此处无需细粒度追踪。
 */
const navChartOption = computed(() => {
  const dates = props.navSeries.map((p) => p.date)
  const navValues = props.navSeries.map((p) => p.nav)
  const ddValues = props.drawdownSeries.map((p) => p.drawdown * 100) // 转为百分比

  return markRaw({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        let html = `<b>${params[0]?.axisValue}</b><br/>`
        for (const p of params) {
          const val = p.seriesName === '最大回撤'
            ? `${p.value.toFixed(2)}%`
            : p.value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
          html += `${p.marker} ${p.seriesName}: ${val}<br/>`
        }
        return html
      },
    },
    legend: {
      data: ['净值曲线', '最大回撤'],
      top: 0,
    },
    grid: {
      left: 80,
      right: 80,
      top: 40,
      bottom: 60,
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: {
        formatter: (val: string) => val.substring(0, 7), // 仅显示年-月
      },
    },
    yAxis: [
      {
        type: 'value',
        name: '净值',
        position: 'left',
        scale: true,
        axisLabel: {
          formatter: (val: number) => val.toLocaleString(),
        },
      },
      {
        type: 'value',
        name: '回撤 (%)',
        position: 'right',
        inverse: true,  // 反转：回撤向下
        axisLabel: {
          formatter: (val: number) => `${val.toFixed(1)}%`,
        },
      },
    ],
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', start: 0, end: 100, height: 20 },
    ],
    series: [
      {
        name: '净值曲线',
        type: 'line',
        yAxisIndex: 0,
        data: navValues,
        smooth: true,
        showSymbol: false,
        // Quant 蓝（与全局 primary 同源），替代 EP 默认亮蓝 #409EFF
        lineStyle: { width: 2, color: '#2962ff' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(41, 98, 255, 0.3)' },
              { offset: 1, color: 'rgba(41, 98, 255, 0.02)' },
            ],
          },
        },
      },
      {
        name: '最大回撤',
        type: 'line',
        yAxisIndex: 1,
        data: ddValues,
        smooth: true,
        showSymbol: false,
        // 回撤红（与 candlestick 阳线同色），替代 EP 默认 #F56C6C
        lineStyle: { width: 1.5, color: '#ef5350' },
        areaStyle: {
          color: 'rgba(239, 83, 80, 0.15)',
        },
      },
    ],
  })
})

/** 权重堆叠面积图配置 */
const weightChartOption = computed(() => {
  if (!props.weightSeries || props.weightSeries.length === 0) return markRaw({})

  const dates = props.weightSeries.map((p) => p.date)

  // 提取所有标代码
  const symbols = Object.keys(props.weightSeries[0].weights)

  // 终端调色板（与 echarts-terminal-dark 注册色一致），替代 EP 默认亮色调色板
  const colors = ['#2962ff', '#26a69a', '#f78166', '#d29922', '#bc8cff']

  // 为每个标的构建权重序列
  const series = symbols.map((symbol, idx) => {
    const data = props.weightSeries!.map((p) =>
      ((p.weights[symbol] ?? 0) * 100) // 转为百分比
    )

    return {
      name: symbol,
      type: 'line' as const,
      stack: 'weight',
      areaStyle: { opacity: 0.6 },
      smooth: true,
      showSymbol: false,
      lineStyle: { width: 1 },
      itemStyle: { color: colors[idx % colors.length] },
      data,
    }
  })

  return markRaw({
    tooltip: {
      trigger: 'axis',
      formatter: (params: any[]) => {
        let html = `<b>${params[0]?.axisValue}</b><br/>`
        for (const p of params) {
          html += `${p.marker} ${p.seriesName}: ${p.value.toFixed(1)}%<br/>`
        }
        return html
      },
    },
    legend: {
      data: symbols,
      top: 0,
    },
    grid: {
      left: 80,
      right: 30,
      top: 40,
      bottom: 60,
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: {
        formatter: (val: string) => val.substring(0, 7),
      },
    },
    yAxis: {
      type: 'value',
      name: '权重 (%)',
      min: 0,
      max: 100,
      axisLabel: {
        formatter: (val: number) => `${val}%`,
      },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', start: 0, end: 100, height: 20 },
    ],
    series,
  })
})
</script>

<style scoped>
.chart-container {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* 暗黑终端：透明底（继承父级极夜黑/卡片底），去亮色阴影，避免白底刺眼 */
.chart-section {
  background: transparent;
  border: 1px solid #2b3139;
  border-radius: 6px;
  padding: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}

.chart-title {
  font-size: 15px;
  font-weight: 600;
  color: #d1d4dc;
  margin: 0 0 12px 0;
}

.chart {
  width: 100%;
  height: 400px;
}
</style>
