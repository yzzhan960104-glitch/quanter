<script setup lang="ts">
/**
 * 执行计划 DAG 图（层级三）
 *
 * 消费 ExecutionPlanNode[]，按 stage 分列左→右布局（data→factor→signal→order），
 * ECharts graph layout='none' + 预计算 x/y，节点按 stage 着色，depends_on 连箭头边。
 * tooltip 展示节点 detail（多行）。
 *
 * 布局算法（纯显式，无 force-directed 黑盒抖动）：
 * - 列号：data=0 / factor=1 / signal=2 / order=3（其它 stage 归到 factor 列）
 * - factor 列多节点纵向堆叠居中；其余列单节点居中
 */
import { computed, markRaw } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import type { ExecutionPlanNode } from '@/api/strategy'

use([CanvasRenderer, GraphChart, TooltipComponent])

const props = defineProps<{ nodes: ExecutionPlanNode[] }>()

// 阶段 → 列号 / 颜色 / 中文名
const STAGE_META: Record<string, { col: number; color: string; cn: string }> = {
  data:   { col: 0, color: '#2962ff', cn: '数据' },
  factor: { col: 1, color: '#26a69a', cn: '因子' },
  signal: { col: 2, color: '#d29922', cn: '信号' },
  order:  { col: 3, color: '#ef5350', cn: '下单' },
}

const COL_X = [80, 300, 520, 740]        // 四列 x 坐标
const CENTER_Y = 180                      // 单节点列的 y
const FACTOR_GAP = 90                     // factor 列节点间距

const chartOption = computed(() => {
  const ns = props.nodes
  if (!ns || !ns.length) return null
  const byCol: Record<number, ExecutionPlanNode[]> = {}
  ns.forEach((n) => {
    const col = (STAGE_META[n.stage] || { col: 1 }).col
    ;(byCol[col] ||= []).push(n)
  })
  // 计算每个节点坐标
  const pos: Record<string, { x: number; y: number }> = {}
  Object.entries(byCol).forEach(([colStr, list]) => {
    const col = Number(colStr)
    const x = COL_X[col] ?? 300
    if (list.length === 1) {
      pos[list[0].id] = { x, y: CENTER_Y }
    } else {
      // 多节点纵向堆叠居中
      const total = (list.length - 1) * FACTOR_GAP
      list.forEach((n, i) => {
        pos[n.id] = { x, y: CENTER_Y - total / 2 + i * FACTOR_GAP }
      })
    }
  })
  // 边：depends_on → node（箭头指向依赖方）
  const links = ns.flatMap((n) =>
    (n.depends_on || []).map((src) => ({ source: src, target: n.id })),
  )
  return markRaw({
    animation: false,
    tooltip: {
      formatter: (p: any) => {
        if (p.dataType === 'node' && p.data) {
          return `<b>${p.data.label}</b><br/>${(p.data.value || '').replace(/\n/g, '<br/>')}`
        }
        return ''
      },
    },
    series: [{
      type: 'graph',
      layout: 'none',
      symbolSize: 46,
      roam: false,
      label: { show: true, position: 'inside', color: '#fff', fontSize: 11, fontWeight: 600 },
      // 节点按 stage 着色（roundRect 接近流程图方块观感）
      itemStyle: { color: (p: any) => STAGE_META[p?.data?.stage]?.color || '#787b86', borderColor: 'transparent' },
      symbol: 'roundRect',
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 10],
      lineStyle: { color: '#5d606b', width: 1.5, curveness: 0.15 },
      data: ns.map((n) => ({
        name: n.id, x: pos[n.id]?.x ?? 0, y: pos[n.id]?.y ?? 0,
        label: n.label, value: n.detail, stage: n.stage,
        itemStyle: { color: (STAGE_META[n.stage] || { color: '#787b86' }).color },
      })),
      links,
    }],
  })
})
</script>

<template>
  <div v-if="!chartOption" class="empty">暂无执行计划节点</div>
  <v-chart v-else class="plan-chart" :option="chartOption" autoresize theme="terminal-dark" />
</template>

<style scoped>
.plan-chart { height: 360px; }
.empty { color: var(--qt-text-secondary); padding: 24px; text-align: center; font-size: 12px; }
</style>
