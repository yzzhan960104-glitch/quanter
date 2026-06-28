<!--
  策略参数动态表单（JSON Schema 驱动）

  职责：
  1. 按 strategy_name 拉取 params_model 的 JSON Schema
  2. 按 schema.properties[*].ui.group 分组为 el-tabs
  3. 按 ui.control 渲染 slider/input-number/select（约束取自 schema，与后端同源）
  4. v-model 双向绑定到 strategyParams（提交时作为请求 strategy_params）

  设计原则：
  - 单一真相源：控件约束（min/max/step/enum）全部取自 schema，前端不重复定义
  - 0-1 浮点字段（如 tech_weight）slider 以百分比展示，回传时 ÷100
-->
<template>
  <div v-if="loading" class="spf-loading">加载策略参数…</div>
  <div v-else-if="!schema || Object.keys(schema.properties).length === 0" class="spf-empty">
    该策略无可调参数
  </div>
  <el-tabs v-else v-model="activeTab" type="border-card">
    <el-tab-pane
      v-for="group in groupedFields"
      :key="group.name"
      :label="group.name"
      :name="group.name"
    >
      <el-form label-position="top">
        <el-form-item
          v-for="key in group.fields"
          :key="key"
          :label="schema.properties[key].description || key"
        >
          <!-- slider -->
          <el-slider
            v-if="getUi(key).control === 'slider'"
            :model-value="toSlider(key)"
            :min="toSliderMin(key)"
            :max="toSliderMax(key)"
            :step="getUi(key).step ?? 1"
            :show-tooltip="true"
            style="width: 100%"
            @update:model-value="(v: number) => fromSlider(key, v)"
          />
          <!-- select -->
          <el-select
            v-else-if="getUi(key).control === 'select'"
            :model-value="strategyParams[key] as string"
            style="width: 100%"
            @update:model-value="(v: string) => setField(key, v)"
          >
            <el-option
              v-for="opt in selectOptions(key)"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <!-- input-number（默认） -->
          <el-input-number
            v-else
            :model-value="strategyParams[key] as number"
            :min="schema.properties[key].minimum"
            :max="schema.properties[key].maximum"
            :step="getUi(key).step ?? 1"
            style="width: 100%"
            @update:model-value="(v: number) => setField(key, v)"
          />
        </el-form-item>
      </el-form>
    </el-tab-pane>
  </el-tabs>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import type { StrategyParamSchema } from '../api/backtest'
import { getStrategySchema } from '../api/backtest'

const props = defineProps<{ strategyName: string }>()
const emit = defineEmits<{ update: [params: Record<string, unknown>] }>()

/** 当前策略参数 JSON Schema */
const schema = ref<StrategyParamSchema | null>(null)
const loading = ref(false)
/** 响应式参数值（提交时整体回传） */
const strategyParams = reactive<Record<string, unknown>>({})
const activeTab = ref('')

/** 拉取 schema 并用默认值初始化 strategyParams */
async function loadSchema(name: string) {
  loading.value = true
  try {
    const s = await getStrategySchema(name)
    schema.value = s
    // 用 schema 默认值初始化（确保缺省提交也有合法值）
    for (const [k, v] of Object.entries(s.properties)) {
      if (v.default !== undefined) strategyParams[k] = v.default
    }
    // 默认激活第一个分组
    const groups = groupedFields.value
    if (groups.length > 0) activeTab.value = groups[0].name
    emit('update', { ...strategyParams })
  } finally {
    loading.value = false
  }
}

watch(() => props.strategyName, (name) => {
  if (name) {
    // 清空旧参数（切换策略时避免残留字段污染）
    Object.keys(strategyParams).forEach((k) => delete strategyParams[k])
    loadSchema(name)
  }
}, { immediate: true })

/** 按 ui.group 分组（无 group 归"其他"），保字段定义顺序 */
const groupedFields = computed(() => {
  if (!schema.value) return []
  const groups: { name: string; fields: string[] }[] = []
  const index: Record<string, number> = {}
  for (const [key, prop] of Object.entries(schema.value.properties)) {
    const gname = prop.ui?.group ?? '其他'
    if (!(gname in index)) {
      index[gname] = groups.length
      groups.push({ name: gname, fields: [] })
    }
    groups[index[gname]].fields.push(key)
  }
  return groups
})

function getUi(key: string) {
  return schema.value?.properties[key]?.ui ?? {}
}

/** select 控件选项：优先 ui.options（含中文 label），否则用 enum */
function selectOptions(key: string) {
  const prop = schema.value!.properties[key]
  return prop.ui?.options ?? (prop.enum ?? []).map((v) => ({ label: v, value: v }))
}

/** 0-1 浮点字段以百分比展示（slider 0-100），否则原值 */
function isPercent(key: string) {
  const p = schema.value!.properties[key]
  return p.type === 'number' && p.minimum === 0 && p.maximum === 1
}
function toSlider(key: string) {
  return isPercent(key) ? Number(strategyParams[key]) * 100 : Number(strategyParams[key])
}
function toSliderMin(key: string) {
  return isPercent(key) ? 0 : schema.value!.properties[key].minimum ?? 0
}
function toSliderMax(key: string) {
  return isPercent(key) ? 100 : schema.value!.properties[key].maximum ?? 100
}
function fromSlider(key: string, v: number) {
  setField(key, isPercent(key) ? v / 100 : v)
}

function setField(key: string, v: unknown) {
  strategyParams[key] = v
  emit('update', { ...strategyParams })
}
</script>

<style scoped>
.spf-loading, .spf-empty {
  padding: 12px;
  color: #909399;
  font-size: 13px;
  text-align: center;
}
</style>
