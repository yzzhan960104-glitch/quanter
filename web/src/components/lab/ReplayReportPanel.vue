<script setup lang="ts">
/**
 * 回放结果渲染面板（/lab 与 /caisen 复用）。
 *
 * 物理定位：纯展示组件——给定 ReplayReport，渲染
 *   ① 统计卡（命中/胜率/平均盈亏比/最大回撤/年化收益/平均持仓）
 *   ② 资金曲线（ECharts LineSeries，标题标年化 CAGR）
 *   ③ 买卖流水表（逐笔 entry/exit/rr/持仓天）
 *   ④ 形态分布（形态命中数徽章）
 *   ⑤ 月度收益（简易 div 柱状，累计盈亏比按 entry 月份聚合）
 *   ⑥ 生产建议（min_rr_ratio 数据驱动推荐文案）
 * 从 CaisenScreenView.vue 抽取（Spec 2 Task 4），零业务逻辑，props 单向驱动。
 *
 * 样式约束：用 --qt-* token；A 股红涨绿跌 --qt-up/--qt-down；ECharts option 内色值裸 hex
 * 为既定例外（canvas 不解析 CSS var）。
 *
 * 红线（CLAUDE.md 反魔法审查）：
 *   - ECharts 按需 use([...]) 注册，与 CaisenScreenView 现状一致（TitleComponent 已在标题用）；
 *   - 月度收益走简易 div 柱（不引 ECharts 增重）；
 *   - 数字展示 font-variant-numeric: tabular-nums（等宽对齐，防抖动）。
 */
import { computed } from 'vue'
// ECharts（资金曲线，vue-echarts 按需注册——与 CaisenScreenView 注册口径完全一致）
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, TitleComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { EquityPoint, ReplayReport } from '@/api/caisen'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, TitleComponent])

/**
 * showTrades：是否渲染内置「买卖流水表」section。
 *
 * 物理意图（Spec 2 Task 6 控制器裁决）：/lab 主画布自身有独立的「买卖日志」区会直接渲染
 * report.trades，若此处也画流水表则同一份 trades 被渲染两遍（UX 重复）。故 /lab 传 false
 * 省略本组件的流水表（资金曲线/统计卡/形态/月度仍渲染），/caisen 不传该 prop → 默认 true →
 * 行为零变化（纯加法 prop，不破坏既有调用方）。
 */
const props = withDefaults(
  defineProps<{ report: ReplayReport; showTrades?: boolean }>(),
  { showTrades: true },
)

// ============ 回放 computed（从 CaisenScreenView 逐字迁入，replayReport.value → props.report） ============

// 月度收益排序（用于柱状展示，按月份时间序）
const sortedMonthlyReturns = computed(() => {
  return Object.entries(props.report.monthly_returns)
    .map(([month, rr]) => ({ month, rr }))
    .sort((a, b) => a.month.localeCompare(b.month))
})

// 形态分布（回放报告里的形态命中数）
const patternDistEntries = computed(() => {
  return Object.entries(props.report.pattern_dist)
})

// 年化收益曲线 ECharts option（equity_curve → LineSeries；标题标年化 CAGR）
// 注：色值用裸 hex（ECharts canvas 不解析 CSS var，既定例外）
const equityChartOption = computed(() => {
  const curve = props.report.equity_curve || []
  const ann = ((props.report.annualized_return || 0) * 100).toFixed(2)
  return {
    title: { text: `资金曲线（年化 ${ann}%）`, left: 'center', textStyle: { color: '#d1d4dc', fontSize: 13 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: curve.map((p: EquityPoint) => (p.date || '').slice(0, 10)), axisLabel: { color: '#888' } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#888', formatter: (v: number) => v.toFixed(2) } },
    series: [{
      name: 'equity', type: 'line', data: curve.map((p: EquityPoint) => p.equity),
      smooth: true, lineStyle: { color: '#0066cc', width: 2 }, areaStyle: { color: 'rgba(0,102,204,0.15)' },
    }],
  }
})

// ============ 辅助：徽章配色（从 CaisenScreenView 逐字复制——流水表/形态分布复用） ============
/** 形态类型徽章：W 底蓝、头肩顶橙、其他灰 */
function patternTagType(p: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (p === 'w_bottom') return 'success'
  if (p === 'head_shoulder') return 'warning'
  if (p === 'triangle_bottom') return 'danger'
  return 'info'
}

/** 形态类型中文名 */
function patternLabel(p: string): string {
  if (p === 'w_bottom') return 'W 底'
  if (p === 'head_shoulder') return '头肩底'
  if (p === 'triangle_bottom') return '收敛三角'
  return p
}
</script>

<template>
  <!-- 回放结果（从 CaisenScreenView .replay-report 块逐字迁入，replayReport → props.report） -->
  <div class="replay-report">
    <div class="report-grid">
      <div class="metric">
        <span class="mk">命中笔数</span>
        <span class="mv">{{ props.report.n_hits }}</span>
      </div>
      <div class="metric">
        <span class="mk">胜率</span>
        <span class="mv" :class="props.report.win_rate >= 0.5 ? 'up' : 'down'">
          {{ (props.report.win_rate * 100).toFixed(1) }}%
        </span>
      </div>
      <div class="metric">
        <span class="mk">平均盈亏比</span>
        <span class="mv">{{ props.report.avg_rr.toFixed(2) }}</span>
      </div>
      <div class="metric">
        <span class="mk">最大回撤</span>
        <span class="mv down">{{ props.report.max_drawdown.toFixed(2) }}</span>
      </div>
      <div class="metric">
        <span class="mk">年化收益</span>
        <span class="mv" :class="props.report.annualized_return >= 0 ? 'up' : 'down'">
          {{ (props.report.annualized_return * 100).toFixed(2) }}%
        </span>
      </div>
      <div class="metric">
        <span class="mk">平均持仓</span>
        <span class="mv">{{ props.report.avg_holding_bars.toFixed(1) }} 日</span>
      </div>
    </div>

    <div class="report-section">
      <div class="block-title">资金曲线（年化收益）</div>
      <v-chart
        v-if="props.report.equity_curve.length"
        class="equity-chart" :option="equityChartOption" autoresize
      />
      <span v-else class="hint">无资金曲线数据（命中 0 笔）</span>
    </div>

    <!-- 买卖流水表：showTrades=false 时省略（/lab 有独立买卖日志区，避免重复渲染 trades） -->
    <div v-if="props.showTrades" class="report-section">
      <div class="block-title">买卖流水（{{ props.report.trades.length }} 笔）</div>
      <el-table :data="props.report.trades" size="small" max-height="320" empty-text="无成交">
        <el-table-column prop="symbol" label="标的" width="100" />
        <el-table-column label="形态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="patternTagType(row.pattern_type)">{{ patternLabel(row.pattern_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="买入" width="160">
          <template #default="{ row }">{{ row.entry_price.toFixed(2) }} @ {{ row.entry_date.slice(0, 10) }}</template>
        </el-table-column>
        <el-table-column label="卖出" width="160">
          <template #default="{ row }">{{ row.exit_price.toFixed(2) }} @ {{ row.exit_date.slice(0, 10) }}</template>
        </el-table-column>
        <el-table-column prop="exit_reason" label="离场" width="100" />
        <el-table-column label="盈亏比" width="80">
          <template #default="{ row }">
            <span :class="row.rr >= 0 ? 'up' : 'down'">{{ row.rr.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="holding_bars" label="持仓(日)" width="80" />
      </el-table>
    </div>

    <div class="report-section">
      <div class="block-title">形态分布</div>
      <div class="dist-row">
        <el-tag
          v-for="[pattern, count] in patternDistEntries"
          :key="pattern"
          :type="patternTagType(pattern)"
          size="small"
        >{{ patternLabel(pattern) }}：{{ count }}</el-tag>
        <span v-if="patternDistEntries.length === 0" class="hint">无命中</span>
      </div>
    </div>

    <div class="report-section">
      <div class="block-title">月度收益（累计盈亏比）</div>
      <div class="monthly-bars">
        <div
          v-for="item in sortedMonthlyReturns" :key="item.month"
          class="monthly-bar"
        >
          <div
            class="bar"
            :class="item.rr >= 0 ? 'up' : 'down'"
            :style="{ height: Math.min(Math.abs(item.rr) * 40, 60) + 'px' }"
            :title="`${item.month}: ${item.rr.toFixed(2)}`"
          ></div>
          <span class="bar-month">{{ item.month.slice(5) }}</span>
        </div>
        <span v-if="sortedMonthlyReturns.length === 0" class="hint">无月度数据</span>
      </div>
    </div>

    <div class="report-section">
      <div class="block-title">生产建议</div>
      <div class="recommendation">{{ props.report.min_rr_ratio_recommendation }}</div>
    </div>
  </div>
</template>

<style scoped>
/* 回放报告区专属样式（从 CaisenScreenView 逐字迁入，保留原 --qt-* token / 裸 hex） */
.replay-report { display: flex; flex-direction: column; gap: var(--qt-space-3); }
.report-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--qt-space-2);
}
.metric {
  display: flex;
  flex-direction: column;
  padding: var(--qt-space-2) var(--qt-space-3);
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
}
.mk { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.mv {
  font-size: 18px;
  font-weight: 700;
  color: var(--qt-text-primary);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.mv.up { color: var(--qt-up); }
.mv.down { color: var(--qt-down); }

.report-section {
  padding: var(--qt-space-2) var(--qt-space-3);
  background: var(--qt-bg-elevated);
  border-radius: var(--qt-radius-sm);
}
.dist-row { display: flex; gap: var(--qt-space-2); flex-wrap: wrap; }

/* 月度收益柱状（简易 div 实现，避免引入 ECharts 增重） */
.monthly-bars {
  display: flex;
  gap: 6px;
  align-items: flex-end;
  height: 80px;
  margin-top: var(--qt-space-2);
  overflow-x: auto;
}
.monthly-bar {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 28px;
}
.bar {
  width: 16px;
  border-radius: 2px 2px 0 0;
}
.bar.up { background: var(--qt-up); }
.bar.down { background: var(--qt-down); }
.bar-month {
  font-size: 10px;
  color: var(--qt-text-secondary);
  font-family: var(--qt-font-mono);
}

.recommendation {
  font-size: var(--qt-fs-body);
  color: var(--qt-text-regular);
  line-height: 1.6;
  margin-top: var(--qt-space-1);
}

/* 资金曲线图（ECharts） */
.equity-chart {
  width: 100%;
  height: 260px;
}

/* 通用展示辅助（与 CaisenScreenView .hint/.block-title 同义，scoped 隔离后组件内自带一份）。
 * 注意：不定义裸 .up/.down——原 CaisenScreenView 也未定义（交易表 rr 跨度继承默认文本色），
 * 为零行为变化保持一致。统计卡/月度柱的涨跌色走限定选择器 .mv.up/.bar.up 等。 */
.hint { font-size: var(--qt-fs-caption); color: var(--qt-text-secondary); }
.block-title {
  font-size: var(--qt-fs-title);
  color: var(--qt-text-primary);
  margin-bottom: var(--qt-space-2);
  font-weight: 600;
}
</style>
