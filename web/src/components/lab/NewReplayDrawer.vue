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
import { PARAM_GROUPS, PARAM_META, CORE_GROUPS, isCoreGroup } from './paramMeta'
import type { ParamGroup } from './paramMeta'
import type { ReplayAsyncRequestBody } from '@/api/caisen'

const props = defineProps<{
  visible: boolean
  configSchema: Record<string, any>
  prefill?: Record<string, unknown> | null
  submitting?: boolean
}>()
const emit = defineEmits<{ 'update:visible': [boolean]; submit: [ReplayAsyncRequestBody] }>()

// 折叠面板默认仅展开形态核心组（CORE_GROUPS：时间跨度/空间高度/量价配合/蔡森方法学）。
// 物理意图：参数瘦身 Task 2 分层——抽屉打开即见「形态核心」参数（策略骨架），高级组
// （交易执行/时间止损/风控）折叠在「显示高级参数」开关后，默认不挡视野。用户可手动展开/收起。
const activeGroups = ref<string[]>([...CORE_GROUPS])

// 「显示高级参数」开关——仅控高级组可见性（交易执行/时间止损/风控），默认 false。
// 语义：开关=false 时高级组不渲染（v-if 卸载），用户看不到也填不了；要调高级参数就开 toggle
// 再填。这符合「默认不挡视野」且不会丢值——默认 false 时本就没填，true 时才出现可填。
// 高级参数仍绑定 cfg、onSubmit 仍收集改过的字段（见 onSubmit 的 Object.entries(cfg.value)，
// 遍历的是 cfg 而非 visibleGroups），故「开关仅控可见性、保留全调参能力」契约成立。
const showAdvanced = ref(false)

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

// 开「显示高级参数」时自动展开高级组——避免「开了开关、高级折叠组出现但仍收起」的两道门
// 粗糙体验（用户既已主动开 toggle，意图明确就是想看/调高级参数，应直接展开）。
// 关闭时同步把高级组移出 activeGroups，保持展开态与可见性一致（无残留副作用）。
watch(showAdvanced, (on) => {
  const advanced = PARAM_GROUPS.filter((g) => !isCoreGroup(g))
  activeGroups.value = on
    ? Array.from(new Set([...activeGroups.value, ...advanced]))
    : activeGroups.value.filter((g) => isCoreGroup(g as ParamGroup))
})

// 按 PARAM_GROUPS 分组的字段（仅含 schema 里实际存在的字段——防御 schema 与 paramMeta 漂移）。
const groupedFields = computed(() =>
  PARAM_GROUPS.map((g) => ({
    group: g,
    fields: Object.entries(PARAM_META)
      .filter(([name, m]) => m.group === g && props.configSchema?.properties?.[name])
      .map(([name, m]) => ({ name, ...m, spec: props.configSchema.properties[name] })),
  })).filter((g) => g.fields.length),
)

// 可见分组：形态核心组恒显；高级组（交易执行/时间止损/风控）仅 showAdvanced=true 时显。
// 物理意图：模板 v-for 用此（而非 groupedFields）渲染，实现「形态核心默认展开、高级组折叠在
// 开关后」的分层。注意 onSubmit 仍遍历 cfg.value（全部已填字段，含高级），与 visibleGroups 解耦——
// 隐藏不等于不收集，保留全调参能力。
const visibleGroups = computed(() =>
  groupedFields.value.filter((g) => isCoreGroup(g.group) || showAdvanced.value),
)

function onSubmit() {
  // 仅传非默认字段（增量覆盖语义，与后端 _merge_cfg 一致）；start/end 必填，
  // universe 留空=全市场（null，后端按全 universe 跑，显式 null 而非 undefined）。
  //
  // 清空=不覆盖（用后端默认），防 null 透传触发 pydantic 422：
  // el-input-number 清空后 cfg[name] 变 null，若该字段无 schema default（defaults[name] 为
  // undefined），null !== undefined 为真会把 null 塞进 cfg_override；后端 model_copy(update=...)
  // 对 float/int 字段收 null 会抛 422。故这里只传「真正改过且非空」的值——
  // 注意：boolean false / 数字 0 是合法非空值，须正常传，不能被过滤掉。
  const cfgOverride: Record<string, any> = {}
  for (const [name, val] of Object.entries(cfg.value)) {
    if (val !== defaults.value[name] && val !== null && val !== undefined) {
      cfgOverride[name] = val
    }
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
    <!-- 形态核心组默认展开；高级组折叠在「显示高级参数」开关后（showAdvanced 仅控可见性） -->
    <div class="drawer-section">
      <el-switch v-model="showAdvanced" active-text="显示高级参数（计划/执行/风控）" size="small" />
    </div>
    <el-collapse v-model="activeGroups" class="drawer-section">
      <el-collapse-item v-for="g in visibleGroups" :key="g.group" :title="g.group" :name="g.group">
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
