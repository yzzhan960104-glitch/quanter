<script setup lang="ts">
/**
 * 策略拓扑与执行计划视图（层级三·路由 /strategies）
 *
 * 三栏编排：
 *   左：策略列表（label + rhythm 徽章），动态反射 /strategies。
 *   右上：拓扑白盒信息（composition.factors/datasets 标签 + rhythm + capital_allocation）。
 *   右中：动态参数表单（复用 StrategyParamForm，按后端 JSON Schema 自动渲染，零硬编码）。
 *   右下：执行计划 DAG 图（ExecutionPlanGraph，数据→因子→信号→下单 生命周期）。
 *
 * 反黑盒：策略清单、参数约束、执行计划全部来自后端，前端只做反射与编排。
 */
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getStrategies, getExecutionPlan, type StrategyTopology, type ExecutionPlan } from '@/api/strategy'
import StrategyParamForm from '@/components/StrategyParamForm.vue'
import ExecutionPlanGraph from '@/components/ExecutionPlanGraph.vue'
import { logger } from '@/utils/logger'

const strategies = ref<StrategyTopology[]>([])
const selected = ref<StrategyTopology | null>(null)
const plan = ref<ExecutionPlan | null>(null)
const loading = ref(false)
const planLoading = ref(false)
// StrategyParamForm 的当前参数（仅预览，Layer 3 不触发回测执行）
const currentParams = ref<Record<string, unknown>>({})

async function fetchStrategies() {
  loading.value = true
  try {
    strategies.value = await getStrategies()
    if (strategies.value.length && !selected.value) {
      selectStrategy(strategies.value[0])
    }
  } catch (e: any) {
    logger.error('策略列表拉取失败:', e)
  } finally {
    loading.value = false
  }
}

async function selectStrategy(s: StrategyTopology) {
  selected.value = s
  plan.value = null
  planLoading.value = true
  try {
    plan.value = await getExecutionPlan(s.name)
  } catch (e: any) {
    ElMessage.error('执行计划拉取失败：' + (e?.message || ''))
  } finally {
    planLoading.value = false
  }
}

function onParamsUpdate(p: Record<string, unknown>) {
  currentParams.value = p
}

onMounted(fetchStrategies)
</script>

<template>
  <div class="sa-view">
    <div class="page-header">
      <div class="title">策略拓扑与执行计划</div>
      <div class="sub">白盒反射策略注册表 · composition/rhythm/capital_allocation · 动态参数表单 + 执行计划 DAG</div>
      <el-button size="small" :loading="loading" @click="fetchStrategies">刷新</el-button>
    </div>

    <div class="sa-body">
      <!-- 左：策略列表 -->
      <aside class="strat-list">
        <div v-if="!strategies.length && !loading" class="empty">暂无注册策略</div>
        <div
          v-for="s in strategies" :key="s.name" class="strat-item"
          :class="{ active: selected?.name === s.name }" @click="selectStrategy(s)"
        >
          <div class="strat-label">{{ s.label }}</div>
          <div class="strat-meta">
            <span class="mono">{{ s.name }}</span>
            <span class="rhythm">{{ s.rhythm }}</span>
          </div>
        </div>
      </aside>

      <!-- 右：详情 -->
      <section v-if="selected" class="strat-detail">
        <!-- 拓扑白盒 -->
        <div class="card">
          <div class="card-title">拓扑白盒</div>
          <div class="topo-row">
            <span class="k">交易节奏</span><span class="v">{{ selected.rhythm }}</span>
          </div>
          <div class="topo-row">
            <span class="k">资金分配</span><span class="v">{{ selected.capital_allocation || '—' }}</span>
          </div>
          <div class="topo-row">
            <span class="k">依赖因子</span>
            <div class="tags">
              <el-tag v-if="!selected.composition?.factors?.length" size="small" type="info">—</el-tag>
              <el-tag v-for="f in selected.composition?.factors || []" :key="f" size="small">{{ f }}</el-tag>
            </div>
          </div>
          <div class="topo-row">
            <span class="k">数据集</span>
            <div class="tags">
              <el-tag v-if="!selected.composition?.datasets?.length" size="small" type="info">—</el-tag>
              <el-tag v-for="d in selected.composition?.datasets || []" :key="d" size="small" effect="plain">{{ d }}</el-tag>
            </div>
          </div>
        </div>

        <!-- 动态参数表单（复用 StrategyParamForm，按后端 JSON Schema 自动渲染） -->
        <div class="card">
          <div class="card-title">动态参数（后端 JSON Schema 驱动）</div>
          <StrategyParamForm :strategy-name="selected.name" @update="onParamsUpdate" />
        </div>

        <!-- 执行计划 DAG -->
        <div class="card">
          <div class="card-title">执行计划图（数据 → 因子 → 信号 → 下单）</div>
          <div v-if="planLoading" v-loading="true" class="plan-loading" />
          <ExecutionPlanGraph v-else-if="plan" :nodes="plan.nodes" />
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.sa-view { flex: 1; overflow: hidden; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.page-header { display: flex; align-items: baseline; gap: 12px; flex-shrink: 0; }
.page-header .title { font-size: 15px; font-weight: 700; color: var(--qt-text-primary); }
.page-header .sub { font-size: 11px; color: var(--qt-text-secondary); flex: 1; }

.sa-body { flex: 1; display: flex; gap: 12px; overflow: hidden; }
.strat-list {
  width: 220px; flex-shrink: 0; overflow-y: auto;
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 6px;
}
.strat-item {
  padding: 8px 10px; border-radius: 4px; cursor: pointer; transition: background 0.15s;
}
.strat-item:hover { background: var(--qt-bg-overlay); }
.strat-item.active { background: rgba(41, 98, 255, 0.15); border-left: 2px solid var(--qt-accent); }
.strat-label { font-size: 13px; color: var(--qt-text-primary); font-weight: 600; }
.strat-meta { display: flex; justify-content: space-between; align-items: center; margin-top: 3px; }
.strat-meta .mono { font-size: 10px; color: var(--qt-text-secondary); font-family: ui-monospace, Menlo, monospace; }
.strat-meta .rhythm { font-size: 10px; color: var(--qt-warn); }

.strat-detail { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.card {
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 10px 12px;
}
.card-title { font-size: 12px; color: var(--qt-text-primary); font-weight: 600; margin-bottom: 8px; }
.topo-row { display: flex; align-items: flex-start; gap: 10px; padding: 3px 0; font-size: 12px; }
.topo-row .k { color: var(--qt-text-secondary); width: 64px; flex-shrink: 0; }
.topo-row .v { color: var(--qt-text-regular); flex: 1; line-height: 1.5; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; flex: 1; }
.plan-loading { height: 360px; }
.empty { color: var(--qt-text-secondary); padding: 24px; text-align: center; font-size: 12px; }
</style>
