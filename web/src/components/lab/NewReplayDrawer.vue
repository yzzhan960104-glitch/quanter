<script setup lang="ts">
/**
 * 新建回测抽屉（/lab「＋新建回测」入口）。
 *
 * 物理定位：可编辑参数表单（configSchema 反射 + paramMeta 中文分组）+ 区间/标的 +
 * 提交。默认从 prefill（当前选中任务 cfg）灌入，便于微调重跑。提交 emit submit，
 * 由父组件 ParamLabView 调 submitReplayAsync。
 *
 * 设计：cfg_override 本地 ref 初始化 = prefill ?? schema defaults；只把「改过/非默认」
 * 的字段进提交体（与后端 _merge_cfg 增量合并语义一致——默认值不传，后端用 StrategyConfig 默认）。
 *
 * 风控拷问·状态机边界：本组件只做表单装配与 emit，不发 HTTP（网络重试/超时由父组件统一处理）；
 * start/end 必填校验在按钮 disabled 上显式守护，杜绝空区间误触发后端 422。
 */
import { computed, ref, watch } from 'vue'
import { PARAM_GROUPS, PARAM_META } from './paramMeta'
import type { ReplayAsyncRequestBody } from '@/api/caisen'

const props = defineProps<{
  visible: boolean
  configSchema: Record<string, any>
  prefill?: Record<string, unknown> | null
  submitting?: boolean
}>()
const emit = defineEmits<{ 'update:visible': [boolean]; submit: [ReplayAsyncRequestBody] }>()

// 折叠面板默认全展开（PARAM_GROUPS 即所有分组名）——抽屉打开即见全部参数，免逐个点击。
// 物理意图：参数多（30+）但分 7 组，默认展开便于横向对照；用户可手动收起不关心分组。
const activeGroups = ref<string[]>([...PARAM_GROUPS])

const start = ref('')
const end = ref('')
const universeText = ref('')              // 文本框（逗号/空白分隔），提交时拆为数组
const cfg = ref<Record<string, any>>({})  // 本地可编辑参数

// schema 默认值表（number/integer/boolean）——用于「仅传非默认」判断 + 抽屉打开时灌默认。
const defaults = computed<Record<string, any>>(() => {
  const out: Record<string, any> = {}
  for (const [name, f] of Object.entries(props.configSchema?.properties || {})) {
    out[name] = (f as any).default
  }
  return out
})

// 抽屉打开 / prefill 变化时重灌：prefill 优先，否则 schema 默认。
// immediate=true 让首次打开（visible=true）即灌入，避免空表单闪现。
watch(
  () => [props.visible, props.prefill],
  () => {
    if (!props.visible) return
    cfg.value = { ...defaults.value, ...(props.prefill || {}) }
  },
  { immediate: true },
)

// 按 PARAM_GROUPS 分组的字段（仅含 schema 里实际存在的字段——防御 schema 与 paramMeta 漂移）。
const groupedFields = computed(() =>
  PARAM_GROUPS.map((g) => ({
    group: g,
    fields: Object.entries(PARAM_META)
      .filter(([name, m]) => m.group === g && props.configSchema?.properties?.[name])
      .map(([name, m]) => ({ name, ...m, spec: props.configSchema.properties[name] })),
  })).filter((g) => g.fields.length),
)

function onSubmit() {
  // 仅传非默认字段（增量覆盖语义，与后端 _merge_cfg 一致）；start/end 必填，
  // universe 留空=全市场（null，后端按全 universe 跑，显式 null 而非 undefined）。
  const cfgOverride: Record<string, any> = {}
  for (const [name, val] of Object.entries(cfg.value)) {
    if (val !== defaults.value[name]) cfgOverride[name] = val
  }
  const universe = universeText.value.trim()
    ? universeText.value.split(/[\s,，]+/).filter(Boolean)
    : null
  emit('submit', { start: start.value, end: end.value, universe, cfg_override: cfgOverride })
}
</script>

<template>
  <el-drawer :model-value="visible" title="新建回测" size="480px"
             @update:model-value="emit('update:visible', $event)">
    <!-- 区间 / 标的池 -->
    <div class="drawer-section">
      <div class="qt-section-title">回测区间与标的</div>
      <el-date-picker v-model="start" type="date" value-format="YYYY-MM-DD" placeholder="开始日" data-testid="start" />
      <el-date-picker v-model="end"   type="date" value-format="YYYY-MM-DD" placeholder="结束日" data-testid="end" />
      <el-input v-model="universeText" type="textarea" :rows="2"
                placeholder="标的池（逗号/空白分隔）；留空=全市场（慢）" />
    </div>

    <!-- 分组参数表单（configSchema 反射 + paramMeta 中文标题） -->
    <el-collapse v-model="activeGroups" class="drawer-section">
      <el-collapse-item v-for="g in groupedFields" :key="g.group" :title="g.group" :name="g.group">
        <div v-for="f in g.fields" :key="f.name" class="param-row">
          <span class="param-label" :title="f.spec?.description">{{ f.title }}</span>
          <!-- bool → switch；number/integer → input-number（带 ge/le 约束）；其余 → input -->
          <el-switch v-if="f.spec?.type === 'boolean'" v-model="cfg[f.name]" />
          <el-input-number v-else-if="['number','integer'].includes(f.spec?.type)"
                           v-model="cfg[f.name]" :min="f.spec?.exclusiveMinimum ?? f.spec?.minimum"
                           :max="f.spec?.maximum" :step="f.spec?.type === 'integer' ? 1 : 0.01"
                           size="small" />
          <el-input v-else v-model="cfg[f.name]" size="small" />
          <span class="param-default">默认 {{ f.spec?.default }}</span>
        </div>
      </el-collapse-item>
    </el-collapse>

    <template #footer>
      <el-button data-testid="submit-replay" type="primary" :loading="submitting"
                 :disabled="!start || !end" @click="onSubmit">提交异步回测</el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.drawer-section { margin-bottom: var(--qt-space-3); }
.param-row { display: flex; align-items: center; gap: var(--qt-space-2); margin: 4px 0; }
.param-label { width: 150px; color: var(--qt-text-regular); font-size: 12px; }
.param-default { color: var(--qt-text-secondary); font-size: 11px; }
</style>
