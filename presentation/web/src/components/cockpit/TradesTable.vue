<!--
  TradesTable —— 交易流水表组件（一期观测运营层 · Task 9）。

  物理意图：驾驶舱「交易流水」卡片。挂载即拉当天实盘流水（queryTrades facade →
  GET /api/v1/trading/trades），el-table 分页展示，方向用 el-tag 徽章着色
  （buy=红/danger 视觉警示买入动作 · sell=绿/success 视觉提示卖出动作）。

  Why 不对 shares/price 调 .toFixed()：TradeRecord 中这两个字段为 number|string
  联合类型（后端 LIVE_TRADE_COLUMNS 可能返回字符串化的 Decimal/BigInt），
  直接 .toFixed() 在字符串分支会抛 TypeError。这里把 el-table-column 留默认插槽渲染，
  EP 会按原样输出，规避类型坑；若后续要统一小数位，需先 Number(x) 再 toFixed。
-->
<template>
  <el-card shadow="never">
    <template #header>
      <div class="flex-between">
        <span>交易流水 · {{ leg === 'exp' ? '实验腿' : '主腿' }}</span>
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="page.trades" size="small" height="320" v-loading="loading">
      <el-table-column prop="symbol" label="标的" width="130" />
      <el-table-column prop="volume" label="数量" width="100" />
      <el-table-column prop="price" label="价格" width="100" />
      <el-table-column prop="execId" label="成交编号" />
    </el-table>
    <!-- 分页仅在总数超过单页上限时出现，避免单页 2 条数据也挂个分页条的视觉噪声。 -->
    <el-pagination
      v-if="page.total > page.limit"
      layout="prev, pager, next"
      :total="page.total"
      :page-size="page.limit"
      :current-page="currentPage"
      @current-change="onPage"
      small
    />
  </el-card>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, inject, watch, type Ref } from 'vue'
import { getTrades, type GmTradeRow } from '../../api/gm'

const loading = ref(false)
const currentPage = ref(1)
// 腿跟随（2026-08-29 多腿方案）：inject LegSelector 的全局腿态；无选择器祖先时
// 缺省 main（组件独立使用/测试场景零依赖）。
const leg = inject<Ref<string>>('cockpit-leg', ref('main'))
// 本地分页（掘金 execrpts 端点一次拉全量，前端切片——日内成交笔数量级 << 1000）。
const all = reactive<{ rows: GmTradeRow[] }>({ rows: [] })
const page = reactive<{ trades: GmTradeRow[]; total: number; limit: number }>({
  trades: [], total: 0, limit: 50,
})

/**
 * 拉取当前页流水。
 *
 * Why try/finally 包裹 loading：queryTrades 抛错时也要把 loading 关掉，
 * 否则按钮永远转圈、用户无法重试——这是 EP v-loading 常见的「假死」坑。
 * 错误本身不在此处吞掉：默认会被 vue 的全局 errorHandler 捕获并打 console，
 * 这里只负责状态机回正。
 */
async function load() {
  loading.value = true
  try {
    all.rows = await getTrades({ leg: leg.value })
    page.total = all.rows.length
    onPage(1)
  } finally {
    loading.value = false
  }
}

// 切腿即重拉（当日流水按账户隔离——主腿/实验腿是两本账）。
watch(leg, load)

/** el-pagination 翻页回调：本地切片。 */
function onPage(p: number) {
  currentPage.value = p
  const start = (p - 1) * page.limit
  page.trades = all.rows.slice(start, start + page.limit)
}

// 挂载即拉当天流水（驾驶舱首屏即用）。
onMounted(load)
</script>

<style scoped>
/* 头部标题/按钮两端对齐：复用全站工具类风格的轻量本地兜底，
   防止未全局引入 .flex-between 时头部样式崩塌。 */
.flex-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
