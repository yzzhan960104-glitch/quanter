<!--
  组合回测页面

  职责：
  1. 左侧参数面板（含动态权重矩阵） + 右侧图表区
  2. 调用 runPortfolioBacktest API
  3. 结果传递给 NavChart（含权重时序）和 MetricCards 组件渲染
  4. 底部交易记录表格

  与 SingleBacktest 的差异：
  - ParamForm mode="portfolio"，渲染多标的/HMM/迟滞阈值配置
  - NavChart 额外接收 weight_series，渲染权重堆叠面积图
  - 交易记录中 symbol 字段有意义（展示标的代码）
-->
<template>
  <div class="portfolio-backtest">
    <el-row :gutter="20">
      <!-- 左侧参数面板 -->
      <el-col :span="6">
        <ParamForm
          mode="portfolio"
          :loading="loading"
          @submit="onSubmit"
        />
      </el-col>

      <!-- 右侧图表区 -->
      <el-col :span="18">
        <!-- 无数据占位 -->
        <el-empty
          v-if="!result"
          description="请设置组合参数并运行回测"
          :image-size="120"
        />

        <!-- 有数据时展示 -->
        <template v-else>
          <!-- 指标卡片 -->
          <MetricCards :metrics="result.metrics" />

          <!-- 净值曲线 + 回撤图 + 权重时序图 -->
          <div style="margin-top: 20px">
            <NavChart
              :nav-series="result.nav_series"
              :drawdown-series="result.drawdown_series"
              :weight-series="result.weight_series"
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
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import ParamForm from '../components/ParamForm.vue'
import NavChart from '../components/NavChart.vue'
import MetricCards from '../components/MetricCards.vue'
import { runPortfolioBacktest, type PortfolioResponse } from '../api/backtest'

const loading = ref(false)
const result = ref<PortfolioResponse | null>(null)

const currentPage = ref(1)
const pageSize = 20

const paginatedTrades = computed(() => {
  if (!result.value) return []
  const start = (currentPage.value - 1) * pageSize
  return result.value.trades.slice(start, start + pageSize)
})

function directionTagType(direction: string): 'success' | 'danger' | 'warning' | 'info' {
  switch (direction) {
    case 'buy': return 'success'
    case 'sell': return 'danger'
    case 'failed': return 'warning'
    default: return 'info'
  }
}

function directionLabel(direction: string): string {
  switch (direction) {
    case 'buy': return '买入'
    case 'sell': return '卖出'
    case 'failed': return '失败'
    default: return direction
  }
}

async function onSubmit(params: any) {
  loading.value = true
  result.value = null
  currentPage.value = 1

  try {
    const res = await runPortfolioBacktest(params)
    result.value = res
    ElMessage.success('组合回测完成')
  } catch {
    // 错误已在 Axios 拦截器中处理
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.portfolio-backtest {
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
