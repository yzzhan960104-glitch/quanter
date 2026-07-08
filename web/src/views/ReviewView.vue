<script setup lang="ts">
/**
 * AI 复盘视图（层级六·路由 /review）
 *
 * 业务目标：引入 GLM，基于实盘日志 + 策略上下文产出 Markdown 诊断报告
 * （做得好的地方 / 滑点·逻辑异常点 / 超参数调整建议）。
 *
 * 数据源二选一（radio 切换）：
 *   - 按日期：从 logs/live_trades.csv 读取（start/end）。
 *   - 粘贴文本：直接贴 CSV 日志。
 * 策略下拉从 /strategies 反射（反硬编码）。
 *
 * Markdown 渲染：自写极简 mdToHtml（标题/粗体/列表/代码块），先 escape HTML 防 XSS，
 * 再 apply 格式化；不引 markdown 重型依赖（Karpathy 极简）。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { diagnose, type ReviewReport } from '@/api/review'
import { getStrategies, type StrategyTopology } from '@/api/strategy'
import { logger } from '@/utils/logger'

const strategies = ref<StrategyTopology[]>([])
const loading = ref(false)
const report = ref<ReviewReport | null>(null)

// 数据源模式：date=按日期读日志，paste=粘贴文本
const sourceMode = ref<'date' | 'paste'>('date')
const dateRange = ref<[string, string]>(lastNDays(30))
const csvText = ref('')
const strategy = ref('')
const metricsText = ref('')  // 可选 JSON 关键指标

function lastNDays(n: number): [string, string] {
  const end = new Date(); const start = new Date(); start.setDate(start.getDate() - n)
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)]
}

onMounted(async () => {
  try {
    strategies.value = await getStrategies()
    if (strategies.value.length) strategy.value = strategies.value[0].name
  } catch (e: any) {
    logger.error('策略列表拉取失败:', e)
  }
})

async function onDiagnose() {
  loading.value = true
  report.value = null
  let metrics: Record<string, unknown> = {}
  if (metricsText.value.trim()) {
    try { metrics = JSON.parse(metricsText.value) }
    catch { ElMessage.warning('关键指标 JSON 解析失败，已忽略'); metrics = {} }
  }
  try {
    report.value = await diagnose({
      ...(sourceMode.value === 'date'
        ? { start: dateRange.value[0], end: dateRange.value[1] }
        : { csv_text: csvText.value }),
      strategy_name: strategy.value || undefined,
      metrics,
    })
    if (!report.value.ok) {
      ElMessage.warning(report.value.reason || '复盘失败：无可用日志')
    } else if (report.value.degraded) {
      ElMessage.warning('LLM 降级模式：' + (report.value.reason || ''))
    } else {
      ElMessage.success('复盘报告已生成')
    }
  } catch (e: any) {
    logger.error('复盘请求失败:', e)
  } finally {
    loading.value = false
  }
}

/** 极简 Markdown → HTML（escape 先行防 XSS；标题/粗体/列表/代码块） */
function mdToHtml(md: string): string {
  // 1. escape HTML（防 LLM 输出含 <script> 等）
  let s = md.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // 2. 代码块 ```...```（保护内容不被后续规则误伤——先抽出占位再还原）
  const codes: string[] = []
  s = s.replace(/```([\s\S]*?)```/g, (_, c) => { codes.push(c); return `\x00CODE${codes.length - 1}\x00` })
  // 3. 标题
  s = s.replace(/^###\s+(.*)$/gm, '<h4>$1</h4>')
       .replace(/^##\s+(.*)$/gm, '<h3>$1</h3>')
       .replace(/^#\s+(.*)$/gm, '<h3>$1</h3>')
  // 4. 粗体
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  // 5. 列表项
  s = s.replace(/^-\s+(.*)$/gm, '<li>$1</li>')
  s = s.replace(/(?:<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`)
  // 6. 还原代码块
  s = s.replace(/\x00CODE(\d+)\x00/g, (_, i) => `<pre>${codes[Number(i)]}</pre>`)
  return s
}

const reportHtml = computed(() => report.value ? mdToHtml(report.value.report) : '')
</script>

<template>
  <div class="rv-view">
    <div class="page-header">
      <div class="title">AI 复盘</div>
      <div class="sub">GLM-4 基于实盘日志 + 策略上下文 → Markdown 诊断报告（做得好 / 异常点 / 调参建议）</div>
    </div>

    <!-- 参数面板 -->
    <div class="panel">
      <div class="form-row">
        <el-select v-model="strategy" placeholder="策略" style="width: 200px">
          <el-option v-for="s in strategies" :key="s.name" :label="s.label" :value="s.name" />
        </el-select>
        <el-radio-group v-model="sourceMode" size="small">
          <el-radio-button label="date">按日期读日志</el-radio-button>
          <el-radio-button label="paste">粘贴日志文本</el-radio-button>
        </el-radio-group>
      </div>
      <div class="form-row">
        <template v-if="sourceMode === 'date'">
          <el-date-picker
            v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" size="small"
            start-placeholder="开始" end-placeholder="结束" style="width: 280px"
          />
        </template>
        <template v-else>
          <el-input
            v-model="csvText" type="textarea" :rows="4" size="small"
            placeholder="粘贴 CSV 格式实盘日志（timestamp,symbol,direction,shares,price,strategy,rationale）"
            style="flex: 1"
          />
        </template>
      </div>
      <div class="form-row">
        <el-input
          v-model="metricsText" size="small" style="width: 400px"
          placeholder='可选：关键指标 JSON，如 {"max_drawdown": -0.12, "sharpe": 1.3}'
        />
        <el-button type="primary" :loading="loading" @click="onDiagnose">生成复盘报告</el-button>
      </div>
    </div>

    <!-- 报告区 -->
    <div v-if="loading" v-loading="true" class="report-loading">GLM 推理中（最长 90s）…</div>
    <template v-else-if="report">
      <div class="report-meta">
        <el-tag v-if="report.degraded" type="warning" size="small" effect="dark">降级模式</el-tag>
        <el-tag v-else type="success" size="small" effect="dark">LLM 已生成</el-tag>
        <span class="model">{{ report.model ? `模型：${report.model}` : '模型：未装配（降级）' }}</span>
      </div>
      <div class="report-body" v-html="reportHtml" />
    </template>
    <div v-else class="empty">配置数据源与策略后点击「生成复盘报告」</div>
  </div>
</template>

<style scoped>
.rv-view { flex: 1; overflow: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.page-header { display: flex; align-items: baseline; gap: 12px; }
.page-header .title { font-size: 15px; font-weight: 700; color: var(--qt-text-primary); }
.page-header .sub { font-size: 11px; color: var(--qt-text-secondary); flex: 1; }

.panel { background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.form-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }

.report-loading { padding: 60px; text-align: center; color: var(--qt-text-secondary); font-size: 13px; }
.report-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.report-meta .model { font-size: 11px; color: var(--qt-text-secondary); font-family: ui-monospace, Menlo, monospace; }
.report-body {
  background: var(--qt-bg-card); border: 1px solid var(--qt-border); border-radius: 6px; padding: 16px 20px;
  color: var(--qt-text-regular); font-size: 13px; line-height: 1.8;
}
.report-body :deep(h3) { color: var(--qt-text-primary); font-size: 15px; margin: 14px 0 6px; }
.report-body :deep(h4) { color: var(--qt-text-primary); font-size: 13px; margin: 12px 0 4px; }
.report-body :deep(strong) { color: var(--qt-accent); }
.report-body :deep(ul) { margin: 4px 0 8px 20px; }
.report-body :deep(li) { margin: 2px 0; }
.report-body :deep(pre) {
  background: var(--qt-bg-page); border: 1px solid var(--qt-border); border-radius: 4px; padding: 8px;
  font-family: ui-monospace, Menlo, monospace; font-size: 11px; overflow-x: auto; color: #8e939d;
}
.empty { color: var(--qt-text-secondary); padding: 48px; text-align: center; font-size: 12px; }
</style>
