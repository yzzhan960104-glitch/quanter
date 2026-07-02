<!--
  绩效指标卡片组件

  职责：
  1. 以 4×2 网格展示核心绩效指标
  2. 正收益绿色、负收益红色、中性灰色
  3. 百分比/倍数/绝对值自动格式化

  设计原则：
  - 接收 metrics 对象，不关心数据来源（单资产/组合通用）
  - 纯展示组件，无交互逻辑
-->
<template>
  <div class="metric-cards">
    <el-card
      v-for="item in displayItems"
      :key="item.label"
      class="metric-card"
      shadow="hover"
    >
      <div class="metric-label">{{ item.label }}</div>
      <div class="metric-value" :class="item.colorClass">
        {{ item.formattedValue }}
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Metrics } from '../api/backtest'

const props = defineProps<{
  metrics: Metrics | null
}>()

/** 指标展示项定义 */
interface DisplayItem {
  label: string
  value: number
  format: 'percent' | 'ratio' | 'currency' | 'integer'
  /** 正值颜色：收益类为绿，风险类始终为红（如回撤），中性指标灰色 */
  colorMode: 'profit' | 'risk' | 'neutral'
}

/** 指标配置列表（4×2 = 8 项） */
const metricDefs: DisplayItem[] = [
  { label: '年化收益率', value: 0, format: 'percent', colorMode: 'profit' },
  { label: '夏普比率', value: 0, format: 'ratio', colorMode: 'profit' },
  { label: '最大回撤', value: 0, format: 'percent', colorMode: 'risk' },
  { label: '卡玛比率', value: 0, format: 'ratio', colorMode: 'profit' },
  { label: '胜率', value: 0, format: 'percent', colorMode: 'neutral' },
  { label: '盈亏比', value: 0, format: 'ratio', colorMode: 'profit' },
  { label: '交易次数', value: 0, format: 'integer', colorMode: 'neutral' },
  { label: '失败交易', value: 0, format: 'integer', colorMode: 'risk' },
]

/** 从 metrics 映射到展示值 */
const displayItems = computed(() => {
  if (!props.metrics) {
    return metricDefs.map((d) => ({
      ...d,
      formattedValue: '--',
      colorClass: 'color-neutral',
    }))
  }

  const m = props.metrics
  const values = [
    m.annual_return,
    m.sharpe_ratio,
    m.max_drawdown,
    m.calmar_ratio,
    m.win_rate,
    m.profit_loss_ratio,
    m.n_trades,
    m.n_failed_trades,
  ]

  return metricDefs.map((def, i) => {
    const val = values[i]
    const formattedValue = formatValue(val, def.format)
    const colorClass = getColorClass(val, def.colorMode)
    return { ...def, value: val, formattedValue, colorClass }
  })
})

/** 格式化数值 */
function formatValue(val: number, format: string): string {
  if (val === null || val === undefined || isNaN(val)) return '--'
  switch (format) {
    case 'percent':
      return `${(val * 100).toFixed(2)}%`
    case 'ratio':
      return val.toFixed(2)
    case 'currency':
      return val.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
    case 'integer':
      return Math.round(val).toString()
    default:
      return val.toString()
  }
}

/** 获取颜色类名 */
function getColorClass(val: number, mode: string): string {
  if (mode === 'risk') return 'color-risk'
  if (mode === 'neutral') return 'color-neutral'
  // profit 模式：正值绿，负值红
  return val >= 0 ? 'color-profit' : 'color-risk'
}
</script>

<style scoped>
.metric-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.metric-card {
  text-align: center;
}

.metric-card :deep(.el-card__body) {
  padding: 16px 12px;
}

.metric-label {
  font-size: 12px;
  color: #787b86;
  margin-bottom: 8px;
  letter-spacing: 0.3px;
}

.metric-value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

/* 盈亏色与 candlestick 同色系：正收益=阴线绿系、亏损/风险=阳线红系，全终端统一。
   注意：此处的「绿=盈利 / 红=亏损」是国际通用的绩效盈亏语义，与 K 线「红涨绿跌」
   分属不同语境（绩效区 vs 行情区），共存于同一终端是行业惯例，不构成冲突。 */
.color-profit {
  color: #26a69a;
}

.color-risk {
  color: #ef5350;
}

.color-neutral {
  color: #787b86;
}

/* 响应式：小屏 2 列 */
@media (max-width: 768px) {
  .metric-cards {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
