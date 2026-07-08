<script setup lang="ts">
/**
 * 末态持仓快照（详情版）
 *
 * 消费 SingleBacktestResponse.positions，竖向 key-value 展示（适合 250px 右栏）：
 * 数量 / 市值 / 成本价 / 浮盈(%) / 建仓 / 持仓天数 / 现金 / 总资产。
 *
 * Why 竖向而非 el-table：右栏仅 250px，横向 8 列放不下；竖向 2×4 grid 紧凑且
 * 每个 key-value 一眼可读。浮盈按正负染色（绩效语义：盈利绿 / 亏损红 / 中性灰，
 * 与 K 线涨跌色系无关，参见 MetricCards 同款约定）。
 *
 * 字段全可选兜底：后端旧响应或字段缺失时显示 '--'，不崩。
 */
import type { PositionRow } from '@/api/backtest'

defineProps<{ positions: PositionRow[] }>()

/** 千分位格式化数值；undefined/NaN → '--' */
const fmt = (v: number | undefined | null, digits = 2) =>
  v === undefined || v === null || isNaN(v)
    ? '--'
    : v.toLocaleString('zh-CN', { maximumFractionDigits: digits })

/** 百分比格式化（入参为小数，如 0.0019 → "0.19%"） */
const pct = (v: number | undefined | null) =>
  v === undefined || v === null || isNaN(v) ? '--' : `${(v * 100).toFixed(2)}%`

/** 浮盈染色 class：正=绿/负=红/零或缺失=中性 */
const pnlClass = (v: number | undefined | null) => {
  if (v === undefined || v === null || isNaN(v) || v === 0) return 'neutral'
  return v > 0 ? 'profit' : 'loss'
}
</script>

<template>
  <div class="pos-card">
    <div class="title">持仓快照</div>
    <div v-if="positions.length === 0" class="empty">暂无持仓</div>
    <div v-else class="pos-list">
      <div v-for="p in positions" :key="p.symbol" class="pos-item">
        <div class="pos-symbol">{{ p.symbol }}</div>
        <div class="kv-grid">
          <div class="kv"><span>数量</span><b>{{ fmt(p.qty, 0) }}</b></div>
          <div class="kv"><span>市值</span><b>{{ fmt(p.market_value) }}</b></div>
          <div class="kv"><span>成本价</span><b>{{ fmt(p.avg_cost) }}</b></div>
          <div class="kv">
            <span>浮盈</span>
            <b :class="pnlClass(p.unrealized_pnl)">
              {{ fmt(p.unrealized_pnl) }} ({{ pct(p.unrealized_pnl_pct) }})
            </b>
          </div>
          <div class="kv"><span>建仓</span><b>{{ p.open_date ?? '--' }}</b></div>
          <div class="kv"><span>持仓(天)</span><b>{{ p.holding_days ?? 0 }}</b></div>
          <div class="kv"><span>现金</span><b>{{ fmt(p.cash) }}</b></div>
          <div class="kv"><span>总资产</span><b>{{ fmt(p.nav) }}</b></div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 透明底：嵌在 TerminalView 右栏悬浮卡片（.panel-right #1e222d）内，避免卡中卡色阶断层 */
.pos-card {
  background: transparent;
  border: 1px solid var(--qt-border);
  border-radius: 6px;
  padding: 6px;
}
.title { color: var(--qt-text-secondary); font-size: 12px; margin-bottom: 4px; }
.empty { color: var(--qt-text-secondary); font-size: 12px; padding: 8px; text-align: center; }

.pos-item { padding: 4px 0; }
/* 多标的（组合模式预留）用虚线分隔 */
.pos-item + .pos-item { border-top: 1px dashed var(--qt-border); margin-top: 4px; padding-top: 6px; }
.pos-symbol { color: var(--qt-text-primary); font-size: 12px; font-weight: 600; margin-bottom: 4px; }

/* 2×4 grid：8 个 key-value 紧凑排在 250px 右栏内 */
.kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1px 8px; }
.kv { display: flex; justify-content: space-between; font-size: 11px; line-height: 18px; }
.kv span { color: var(--qt-text-secondary); }
.kv b { color: var(--qt-text-primary); font-variant-numeric: tabular-nums; font-weight: 500; }

/* 浮盈染色：与 MetricCards 同款绩效盈亏语义（绿=盈利 / 红=亏损），非 K 线涨跌色 */
.kv b.profit { color: var(--qt-down); }
.kv b.loss { color: var(--qt-up); }
.kv b.neutral { color: var(--qt-text-primary); }
</style>
