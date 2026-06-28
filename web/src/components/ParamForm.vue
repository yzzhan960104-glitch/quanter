<!--
  参数表单组件

  职责：
  1. 根据模式（single/portfolio）动态渲染不同的表单字段
  2. 表单校验：初始资金 > 0、日期合法性、权重和 = 1
  3. 组合模式的动态权重矩阵表单（symbols × states）
  4. 提交时 emit 事件，由父组件调用 API

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
    <!-- 单资产模式：标的代码 -->
    <el-form-item v-if="mode === 'single'" label="标的代码" prop="symbol">
      <el-input v-model="formData.symbol" placeholder="如 600000.SH" />
    </el-form-item>

    <!-- 组合模式：标的列表 -->
    <el-form-item v-if="mode === 'portfolio'" label="ETF 标的列表" prop="symbols">
      <el-select
        v-model="formData.symbols"
        multiple
        filterable
        allow-create
        default-first-option
        placeholder="输入 ETF 代码后回车添加"
        style="width: 100%"
      >
        <el-option label="510300.SH（沪深300ETF）" value="510300.SH" />
        <el-option label="511010.SH（国债ETF）" value="511010.SH" />
        <el-option label="510500.SH（中证500ETF）" value="510500.SH" />
        <el-option label="518880.SH（黄金ETF）" value="518880.SH" />
      </el-select>
    </el-form-item>

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

/** 表单数据 */
const formData = reactive({
  // 单资产
  symbol: '600000.SH',
  signal_freq: '1d',
  // 策略选择（前端驱动调参）；tech_weights 已下沉到 strategy_params.tech_weight
  strategy_name: 'tech_macro_fusion',
  strategy_params: {} as Record<string, unknown>,
  // 通用
  dateRange: ['2023-01-01', '2024-12-31'] as string[],
  initial_capital: 1000000,
  // 组合
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

/** 表单校验规则 */
const formRules: FormRules = {
  symbol: [{ required: true, message: '请输入标的代码', trigger: 'blur' }],
  symbols: [{ required: true, message: '请至少选择一个标的', trigger: 'change' }],
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
      symbol: formData.symbol,
      start_date: formData.dateRange[0],
      end_date: formData.dateRange[1],
      initial_capital: formData.initial_capital,
      signal_freq: formData.signal_freq,
      strategy_name: formData.strategy_name,
      strategy_params: formData.strategy_params,
    })
  } else {
    emit('submit', {
      symbols: formData.symbols,
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
.param-form {
  padding: 16px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}

/* tech_weights 相关样式（.weight-row / .weight-label）已随滑块块移除 */

.threshold-hint {
  font-size: 12px;
  color: #909399;
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
  border-bottom: 1px solid #ebeef5;
}

.state-label {
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
}
</style>
