<!--
  单资产回测页面

  职责：
  1. 左侧参数面板 + 右侧图表区的经典布局
  2. 调用 runSingleBacktest API
  3. 结果传递给 NavChart 和 MetricCards 组件渲染
  4. 底部交易记录表格

  设计原则：
  - 页面只做数据流编排，不包含图表/表格渲染逻辑
  - loading 状态由页面控制，传递给 ParamForm
-->
<template>
  <div class="single-backtest">
    <el-row :gutter="20">
      <!-- 左侧参数面板 -->
      <el-col :span="6">
        <ParamForm
          mode="single"
          :loading="loading"
          @submit="onSubmit"
        />
      </el-col>

      <!-- 右侧图表区 -->
      <el-col :span="18">
        <!-- 无数据时的占位提示 -->
        <el-empty
          v-if="!result"
          description="请设置参数并运行回测"
          :image-size="120"
        />

        <!-- 有数据时展示图表和指标 -->
        <template v-else>
          <!-- 指标卡片 -->
          <MetricCards :metrics="result.metrics" />

          <!-- 净值曲线 + 回撤图 -->
          <div style="margin-top: 20px">
            <NavChart
              :nav-series="result.nav_series"
              :drawdown-series="result.drawdown_series"
            />
          </div>

          <!-- 交易记录表格 -->
          <div class="trades-section">
            <h3 class="section-title">交易记录</h3>
            <el-table
              :data="paginatedTrades"
              stripe
              border
              size="small"
              style="width: 100%"
            >
              <el-table-column prop="date" label="日期" width="120" />
              <el-table-column prop="direction" label="方向" width="80">
                <template #default="{ row }">
                  <el-tag
                    :type="directionTagType(row.direction)"
                    size="small"
                  >
                    {{ directionLabel(row.direction) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="shares" label="股数" width="100" align="right" />
              <el-table-column prop="price" label="成交价" width="100" align="right">
                <template #default="{ row }">
                  {{ row.price.toFixed(2) }}
                </template>
              </el-table-column>
              <el-table-column prop="cost" label="交易成本" width="120" align="right">
                <template #default="{ row }">
                  {{ row.cost.toFixed(2) }}
                </template>
              </el-table-column>
            </el-table>

            <!-- 分页 -->
            <div class="pagination-wrapper">
              <el-pagination
                v-model:current-page="currentPage"
                :page-size="pageSize"
                :total="result.trades.length"
                layout="total, prev, pager, next"
                small
              />
            </div>
          </div>
        </template>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, computed, triggerRef } from 'vue'
import { ElMessage } from 'element-plus'
import ParamForm from '../components/ParamForm.vue'
import NavChart from '../components/NavChart.vue'
import MetricCards from '../components/MetricCards.vue'
import { runSingleBacktest, type SingleBacktestResponse } from '../api/backtest'

/** 回测加载状态 */
const loading = ref(false)

/**
 * 回测结果（使用 shallowRef 避免 Vue 深度代理灾难）
 *
 * 为什么不用 ref()？
 * ref() 会对整个响应对象做深度 Proxy 代理：
 * - nav_series: 750+ NavPoint → 750+ 独立 Proxy 对象
 * - drawdown_series: 750+ DrawdownPoint → 750+ 独立 Proxy 对象
 * - trades: N 个 TradeRecord → N 个独立 Proxy 对象
 * 初始化耗时可达数百毫秒，且每次属性访问都经 Proxy 拦截。
 *
 * shallowRef 只代理 .value 本身的引用，不递归代理内部属性。
 * 回测结果从后端获取后是只读数据，不需要细粒度响应式追踪，
 * shallowRef 是海量只读时序数据的正确选择。
 */
const result = shallowRef<SingleBacktestResponse | null>(null)

/** 交易记录分页 */
const currentPage = ref(1)
const pageSize = 20

/** 当前页的交易记录 */
const paginatedTrades = computed(() => {
  if (!result.value) return []
  const start = (currentPage.value - 1) * pageSize
  return result.value.trades.slice(start, start + pageSize)
})

/** 交易方向标签类型 */
function directionTagType(direction: string): 'success' | 'danger' | 'warning' | 'info' {
  switch (direction) {
    case 'buy': return 'success'
    case 'sell': return 'danger'
    case 'failed': return 'warning'
    default: return 'info'
  }
}

/** 交易方向中文标签 */
function directionLabel(direction: string): string {
  switch (direction) {
    case 'buy': return '买入'
    case 'sell': return '卖出'
    case 'failed': return '失败'
    default: return direction
  }
}

/** 提交回测请求 */
async function onSubmit(params: any) {
  loading.value = true
  // shallowRef 触发更新：必须整体替换 .value（不能修改内部属性）
  result.value = null
  currentPage.value = 1

  try {
    const res = await runSingleBacktest(params)
    // 整体替换 .value → shallowRef 自动触发组件更新
    result.value = res
    ElMessage.success('回测完成')
  } catch {
    // 错误已在 Axios 拦截器中处理
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.single-backtest {
  min-height: calc(100vh - 120px);
}

.trades-section {
  margin-top: 20px;
  background: #fff;
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 12px 0;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
