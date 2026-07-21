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
        <span>交易流水</span>
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="page.trades" size="small" height="320" v-loading="loading">
      <el-table-column prop="timestamp" label="时间" width="150" />
      <el-table-column prop="symbol" label="标的" width="110" />
      <el-table-column label="方向" width="80">
        <template #default="{ row }">
          <el-tag
            :type="row.direction === 'buy' ? 'danger' : 'success'"
            size="small"
          >
            {{ row.direction }}
          </el-tag>
        </template>
      </el-table-column>
      <!-- shares/price 联合类型：留默认插槽渲染，不做数值格式化（见组件头注释）。 -->
      <el-table-column prop="shares" label="数量" width="80" />
      <el-table-column prop="price" label="价格" width="80" />
      <el-table-column prop="strategy" label="策略" />
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
import { ref, reactive, onMounted } from 'vue'
// 路径修正：本文件在 src/components/cockpit/，src/api/trading.ts 是 ../../api/trading。
// brief Step 3 骨架写的 '../../../api/trading' 多退一层会跳出 src 目录，此处显式修正。
import { queryTrades, type TradesPage } from '../../api/trading'

// 当天日期（YYYY-MM-DD）：流水查询窗口默认只看今天，与驾驶舱「观测运营层」定位一致。
const today = new Date().toISOString().slice(0, 10)

const loading = ref(false)
const currentPage = ref(1)
// 分页响应初值：空 trades + total 0，保证首帧渲染不报错、el-table 显示空态。
const page = reactive<TradesPage>({ trades: [], total: 0, limit: 100, offset: 0 })

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
    const r = await queryTrades({
      start: today,
      end: today,
      limit: 100,
      offset: (currentPage.value - 1) * 100,
    })
    Object.assign(page, r)
  } finally {
    loading.value = false
  }
}

/** el-pagination 翻页回调：更新当前页后重拉。 */
function onPage(p: number) {
  currentPage.value = p
  load()
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
