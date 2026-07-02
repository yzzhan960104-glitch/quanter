<!--
  参数表单组件（Epic 2：去主观化动态标的池）

  职责：
  1. 顶部只读 Universe Card：声明当前运行池 = 宏观动能 Top 50 活跃池（盲打）
  2. 通用回测参数：回测区间 / 初始资金 / 信号频率 / 策略 / 策略参数（single）
     或 HMM 状态数 / 迟滞阈值 / 状态权重矩阵（portfolio）
  3. 表单校验：初始资金 > 0、日期合法性、权重和 = 1（portfolio）
  4. 提交时 emit 事件，由父组件调用 API；symbol/symbols 已被劫持为动态池代号

  设计原则：
  - 校验逻辑尽量在前端完成，减少无效请求
  - 权重和实时校验并红色提示
  - 提交按钮带 loading 状态，防止重复提交
-->
<template>
  <el-form
    ref="formRef"
    :model="formData"
    :rules="formRules"
    label-width="100px"
    label-position="top"
    class="param-form"
    @submit.prevent
  >
    <!--
      ===== Epic 2：动态标的池卡片（Universe Card，去主观化盲打） =====
      真正的量化系统不依赖人工录入个股代码：标的池由后端数据湖按宏观动能自动筛出。
      此卡片为只读信息层，向用户声明「当前运行池 = 宏观动能 Top 50 活跃池」，
      实际标的解析在后端完成（server/api/v1/macro.py 的 [:50] 活跃股池逻辑）。
      右上角闪烁绿点 = 「动态同步中」的活体指示，纯 CSS 动画，零 JS 开销。
    -->
    <div class="universe-card">
      <div class="universe-head">
        <span class="universe-title">策略运行池 (Universe)</span>
        <span class="universe-sync">
          <span class="sync-dot" aria-hidden="true"></span>
          动态同步中
        </span>
      </div>
      <div class="universe-body">
        <span class="universe-core">⚡ 宏观动能 Top 50 活跃池</span>
        <p class="universe-desc">
          系统基于后端数据湖自动读取标的，按宏观动能 + 流动性动态筛选，无需手动指定。
        </p>
      </div>
    </div>

    <!-- 日期范围 -->
    <el-form-item label="回测区间" prop="dateRange">
      <el-date-picker
        v-model="formData.dateRange"
        type="daterange"
        range-separator="至"
        start-placeholder="起始日期"
        end-placeholder="结束日期"
        value-format="YYYY-MM-DD"
        style="width: 100%"
      />
    </el-form-item>

    <!-- 初始资金 -->
    <el-form-item label="初始资金" prop="initial_capital">
      <el-input-number
        v-model="formData.initial_capital"
        :min="10000"
        :step="100000"
        :controls="false"
        style="width: 100%"
      />
    </el-form-item>

    <!-- 信号频率（仅单资产） -->
    <el-form-item v-if="mode === 'single'" label="信号频率" prop="signal_freq">
      <el-select v-model="formData.signal_freq" style="width: 100%">
        <el-option label="日线（1d）" value="1d" />
        <el-option label="小时线（1h）" value="1h" />
        <el-option label="5分钟（5m）" value="5m" />
        <el-option label="1分钟（1m）" value="1m" />
      </el-select>
    </el-form-item>

    <!-- 融合权重块已移除：tech_weights 下沉到 StrategyParamForm 动态渲染（tech_weight 滑块） -->

    <!-- 策略选择（仅单资产） -->
    <el-form-item v-if="mode === 'single'" label="策略" prop="strategy_name">
      <el-select
        v-model="formData.strategy_name"
        placeholder="选择策略"
        style="width: 100%"
      >
        <el-option
          v-for="s in strategies"
          :key="s.name"
          :label="s.label"
          :value="s.name"
        />
      </el-select>
    </el-form-item>

    <!-- 策略参数（动态 schema 渲染，仅单资产） -->
    <el-form-item v-if="mode === 'single'" label="策略参数">
      <StrategyParamForm
        :strategy-name="formData.strategy_name"
        @update="onStrategyParamsUpdate"
      />
    </el-form-item>

    <!-- HMM 状态数（仅组合） -->
    <el-form-item v-if="mode === 'portfolio'" label="HMM 状态数" prop="n_hmm_states">
      <el-input-number v-model="formData.n_hmm_states" :min="2" :max="10" style="width: 100%" />
    </el-form-item>

    <!-- 迟滞阈值（仅组合） -->
    <el-form-item v-if="mode === 'portfolio'" label="迟滞阈值" prop="buffer_threshold">
      <el-slider
        v-model="bufferSliderValue"
        :min="1"
        :max="50"
        :step="1"
        :show-tooltip="true"
        :format-tooltip="(val: number) => `${(val / 100).toFixed(2)}`"
        style="width: 100%"
      />
      <div class="threshold-hint">
        当前值：{{ (bufferSliderValue / 100).toFixed(2) }}（权重偏离超过此值才调仓）
      </div>
    </el-form-item>

    <!-- 状态权重矩阵（仅组合） -->
    <el-form-item v-if="mode === 'portfolio'" label="状态权重配置">
      <div class="state-matrix">
        <el-alert
          v-if="!matrixValid"
          :title="matrixError"
          type="error"
          :closable="false"
          show-icon
          style="margin-bottom: 8px"
        />
        <table class="matrix-table">
          <thead>
            <tr>
              <th>状态</th>
              <th v-for="sym in formData.symbols" :key="sym">{{ sym }}</th>
              <th>行和</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="si in formData.n_hmm_states" :key="si">
              <td class="state-label">State_{{ si - 1 }}</td>
              <td v-for="sym in formData.symbols" :key="sym">
                <el-input-number
                  v-model="formData.state_weights[`State_${si - 1}`][sym]"
                  :min="0"
                  :max="1"
                  :step="0.1"
                  :precision="2"
                  :controls="false"
                  size="small"
                  style="width: 80px"
                />
              </td>
              <td>
                <el-tag
                  :type="rowSum(si - 1) ? 'success' : 'danger'"
                  size="small"
                >
                  {{ rowSumDisplay(si - 1) }}
                </el-tag>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </el-form-item>

    <!-- 运行按钮 -->
    <el-form-item>
      <el-button
        type="primary"
        :loading="loading"
        @click="handleSubmit"
        style="width: 100%"
      >
        {{ loading ? '回测执行中...' : '运行回测' }}
      </el-button>
    </el-form-item>
  </el-form>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
// Task 9：策略选择 + 动态参数表单（前端驱动调参）
import StrategyParamForm from './StrategyParamForm.vue'
import { getStrategies, type StrategyMeta } from '../api/backtest'

const props = defineProps<{
  mode: 'single' | 'portfolio'
  loading?: boolean
}>()

const emit = defineEmits<{
  submit: [formData: any]
}>()

const formRef = ref<FormInstance>()

/**
 * 表单数据
 *
 * Epic 2 去 subjective 化后：
 * - symbol / symbols 不再暴露输入框（顶部 Universe Card 取代），但字段保留为内部
 *   默认值——portfolio 权重矩阵（rebuildStateWeights / state_weights）仍以
 *   formData.symbols 为列维度构建。portfolio 模式当前未挂载任何视图（仅
 *   TerminalView 用 single），故默认值不影响主路径，保留可避免连带改造权重矩阵。
 * - 提交时 handleSubmit 会把 symbol/symbols 劫持为动态池代号，覆盖此处的默认值。
 */
const formData = reactive({
  // 单资产（默认值仅为内部占位，提交时被劫持为 'dynamic_top50'）
  symbol: '600000.SH',
  signal_freq: '1d',
  // 策略选择（前端驱动调参）；tech_weights 已下沉到 strategy_params.tech_weight
  strategy_name: 'tech_macro_fusion',
  strategy_params: {} as Record<string, unknown>,
  // 通用
  dateRange: ['2023-01-01', '2024-12-31'] as string[],
  initial_capital: 1000000,
  // 组合（默认值仅供权重矩阵构建，提交时被劫持为 ['dynamic_top50']）
  symbols: ['510300.SH', '511010.SH'] as string[],
  n_hmm_states: 3,
  buffer_threshold: 0.05,
  state_weights: {
    'State_0': { '510300.SH': 0.8, '511010.SH': 0.2 },
    'State_1': { '510300.SH': 0.2, '511010.SH': 0.8 },
    'State_2': { '510300.SH': 0.5, '511010.SH': 0.5 },
  } as Record<string, Record<string, number>>,
})

/** 迟滞阈值滑块值（1-50 映射到 0.01-0.50） */
const bufferSliderValue = ref(5)

// 监听迟滞阈值滑块值变化，同步到 formData
watch(bufferSliderValue, (val) => {
  formData.buffer_threshold = val / 100
})

/**
 * 策略列表（启动时拉取，填充策略下拉框）
 *
 * 失败静默（catch）：后端未就绪时下拉框为空，用户可看到错误来源在后端而非前端。
 */
const strategies = ref<StrategyMeta[]>([])
getStrategies().then((list) => { strategies.value = list }).catch(() => {})

/**
 * StrategyParamForm 子组件回传参数（合并到 formData.strategy_params）
 *
 * 子组件按 schema 默认值初始化，故用户即使不操作也携带合法缺省值。
 */
function onStrategyParamsUpdate(params: Record<string, unknown>) {
  formData.strategy_params = params
}

/** 监听 symbols/n_hmm_states 变化，重建 state_weights 矩阵 */
watch(
  () => [formData.symbols, formData.n_hmm_states],
  () => {
    rebuildStateWeights()
  },
  { deep: true }
)

/** 重建状态权重矩阵 */
function rebuildStateWeights() {
  const newStateWeights: Record<string, Record<string, number>> = {}
  const n = formData.symbols.length

  for (let i = 0; i < formData.n_hmm_states; i++) {
    const key = `State_${i}`
    const existing = formData.state_weights[key]

    if (existing) {
      // 尝试保留旧值，补充新增标的
      newStateWeights[key] = {}
      const oldSum = formData.symbols.reduce((s, sym) => s + (existing[sym] ?? 0), 0)
      for (const sym of formData.symbols) {
        if (existing[sym] !== undefined) {
          newStateWeights[key][sym] = existing[sym]
        } else {
          // 新增标的分配等权
          newStateWeights[key][sym] = oldSum > 0 ? 0 : +(1 / n).toFixed(2)
        }
      }
    } else {
      // 新增状态：等权分配
      newStateWeights[key] = {}
      for (const sym of formData.symbols) {
        newStateWeights[key][sym] = +(1 / n).toFixed(2)
      }
    }
  }

  formData.state_weights = newStateWeights
}

/** 计算某状态行权重和 */
function rowSum(stateIdx: number): boolean {
  const key = `State_${stateIdx}`
  const weights = formData.state_weights[key]
  if (!weights) return false
  const sum = Object.values(weights).reduce((a, b) => a + b, 0)
  return Math.abs(sum - 1.0) < 0.015  // 允许 0.015 的浮点误差
}

/** 行和展示 */
function rowSumDisplay(stateIdx: number): string {
  const key = `State_${stateIdx}`
  const weights = formData.state_weights[key]
  if (!weights) return '0'
  const sum = Object.values(weights).reduce((a, b) => a + b, 0)
  return sum.toFixed(2)
}

/** 矩阵整体是否合法 */
const matrixValid = computed(() => {
  if (props.mode !== 'portfolio') return true
  if (formData.symbols.length === 0) return false
  for (let i = 0; i < formData.n_hmm_states; i++) {
    if (!rowSum(i)) return false
  }
  return true
})

const matrixError = computed(() => {
  if (formData.symbols.length === 0) return '请至少选择一个标的'
  return '每行权重和必须等于 1.00'
})

/**
 * 表单校验规则
 *
 * Epic 2：已删除 symbol / symbols 的 required 规则（输入框已移除，标的由动态池接管）。
 * 保留 dateRange / initial_capital / signal_freq 的合法性校验。
 */
const formRules: FormRules = {
  dateRange: [{ required: true, message: '请选择回测日期范围', trigger: 'change' }],
  initial_capital: [
    { required: true, message: '请输入初始资金', trigger: 'blur' },
    { type: 'number', min: 1, message: '初始资金必须为正数', trigger: 'blur' },
  ],
  signal_freq: [{ required: true, message: '请选择信号频率', trigger: 'change' }],
}

/** 提交表单 */
async function handleSubmit() {
  if (!formRef.value) return

  // 前端校验
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    ElMessage.warning('请检查表单参数')
    return
  }

  // 组合模式额外校验权重矩阵
  if (props.mode === 'portfolio' && !matrixValid.value) {
    ElMessage.error('状态权重矩阵校验失败：每行权重和必须等于 1')
    return
  }

  // 构建提交数据
  if (props.mode === 'single') {
    emit('submit', {
      /*
       * Epic 2 负载劫持：去主观化盲打，无视用户输入，强制下发动态池子代号。
       *
       * Why 字符串 'dynamic_top50' 而非数组：后端 BacktestRequest.symbol 字段
       *   类型是 str（server/schemas/backtest.py: symbol: str = Field(...)），
       *   若传数组 ["dynamic_top50"] 会触发 Pydantic 422（str 收到 list）。
       *   这里守 str 契约——后端仍收到 { "symbol": "dynamic_top50", ... }，
       *   只是值由单一代号路由到 Top50 活跃池逻辑，符合「零破坏原则」。
       *   （portfolio 分支的 symbols: string[] 契约允许数组，故用 ['dynamic_top50']。）
       */
      symbol: 'dynamic_top50',
      start_date: formData.dateRange[0],
      end_date: formData.dateRange[1],
      initial_capital: formData.initial_capital,
      signal_freq: formData.signal_freq,
      strategy_name: formData.strategy_name,
      strategy_params: formData.strategy_params,
    })
  } else {
    emit('submit', {
      // Epic 2 负载劫持：组合模式 symbols: string[] 契约允许数组，直接下发池子代号数组
      symbols: ['dynamic_top50'],
      start_date: formData.dateRange[0],
      end_date: formData.dateRange[1],
      initial_capital: formData.initial_capital,
      n_hmm_states: formData.n_hmm_states,
      buffer_threshold: formData.buffer_threshold,
      state_weights: formData.state_weights,
      strategy_params: formData.strategy_params,
    })
  }
}
</script>

<style scoped>
/*
 * 表单容器：暗黑终端透明底（继承父卡片 #1e222d，悬浮卡片由 TerminalView .panel 提供）。
 * 修复历史残留：原 background:#fff 是亮色后台管理风格，与暗黑终端冲突。
 */
.param-form {
  padding: 16px;
  background: transparent;
  border-radius: 0;
  box-shadow: none;
}

/* ===== Epic 2：动态标的池卡片（Universe Card） ===== */
.universe-card {
  margin-bottom: 16px;
  padding: 12px 14px;
  background: linear-gradient(135deg, #1e222d 0%, #232731 100%);
  border: 1px solid #2b3139;
  border-left: 3px solid #2962ff;   /* Quant 蓝左边条，锚定「策略核心配置」语义 */
  border-radius: 6px;
}

.universe-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.universe-title {
  font-size: 12px;
  font-weight: 600;
  color: #b2b5be;
  letter-spacing: 0.5px;
}

/* 「动态同步中」活体指示：闪烁绿点 + 文案 */
.universe-sync {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #26a69a;   /* 与 candlestick 阴线绿同色，传达「活跃/正常」状态 */
}

/* 闪烁绿点：1.6s 无限呼吸，模拟数据湖实时同步心跳（纯 CSS，零 JS 开销） */
.sync-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #26a69a;
  box-shadow: 0 0 6px rgba(38, 166, 154, 0.8);
  animation: sync-pulse 1.6s ease-in-out infinite;
}

@keyframes sync-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.35; transform: scale(0.7); }
}

/* 无障碍：尊重 prefers-reduced-motion，用户系统关闭动画时停止闪烁，避免眩晕 */
@media (prefers-reduced-motion: reduce) {
  .sync-dot { animation: none; }
}

.universe-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.universe-core {
  font-size: 14px;
  font-weight: 700;
  color: #d1d4dc;
}

.universe-desc {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: #787b86;
}

/* tech_weights 相关样式（.weight-row / .weight-label）已随滑块块移除 */

.threshold-hint {
  font-size: 12px;
  color: #787b86;
  margin-top: 4px;
}

.state-matrix {
  width: 100%;
}

.matrix-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.matrix-table th,
.matrix-table td {
  padding: 6px 4px;
  text-align: center;
  border-bottom: 1px solid #2b3139;
}

.state-label {
  font-weight: 600;
  color: #d1d4dc;
  white-space: nowrap;
}
</style>
