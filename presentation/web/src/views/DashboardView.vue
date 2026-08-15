<script setup lang="ts">
/**
 * 活跃股池驾驶舱（路由 /dashboard）
 *
 * 定位：股池快照前端视图，呈现「活跃股池」单层信息，供研究员快速浏览
 * 当前湖内活跃标的。
 *
 * 单块布局：
 *   ① 活跃股池表（/macro/pool 端点 pool 字段，daily 内存湖前 50 只）
 *
 * 离线降级红线（贯穿全视图）：
 *   后端在无 daily 湖时返空结构（pool: []），本视图做空态兜底
 *   （TerminalWatermark 极简水印），绝不白屏。原因：daily 湖依赖离线同步，
 *   开发机/CI 默认可能无数据，前端必须能渲染空骨架供联调，避免「无数据 =
 *   整页崩」。
 *
 * 数据加载策略：
 *   onMounted 拉 /macro/pool 端点（单端点，无并发必要，但保留 async 便于扩展）。
 *
 * 历史：原 regime/credit 两块面板已随 CreditRegime 与对应后端端点（/macro/regime、
 * /macro/credit）于 2026-07 整体下线删除；factors 端点亦于 T7 架构治理批 2 删除
 * （前端无调用方）；板块资金流图块（sector/flow 端点 sectors 字段）于 2026-08-15
 * CR-8 处置删除——sector 湖 2026-07-27 退役后板块数据结构性恒空，图块长期渲染
 * 空态水印，本波次确认下线（同步删后端 sector 腿，端点收缩为 /macro/pool）。
 */
import { ref, computed, onMounted } from 'vue'
import TerminalWatermark from '../components/TerminalWatermark.vue'
import { getActivePool, type ActivePoolResponse } from '../api/macro'

// ============ 响应式状态 ============

/** 活跃股池（/macro/pool 端点） */
const poolData = ref<ActivePoolResponse>({ pool: [] })
/** 全局加载态（端点落定前为 true） */
const loading = ref(true)

// ============ 数据加载 ============

async function loadDashboard() {
  loading.value = true
  // 单端点：rejected 时保持空态兜底（拦截器已弹 ElMessage，这里静默吸收避免重复提示）
  try {
    const data = await getActivePool()
    poolData.value = data
  } catch {
    // 保持空态兜底，不抛错
  }
  loading.value = false
}

onMounted(loadDashboard)

// ============ ① 活跃股池表 ============

/**
 * 活跃股池表行（daily 内存湖前 50 只）。后端 pool 元素是 {symbol} 记录（与
 * tests/test_macro_api.py 契约一致）；转成表格行结构，预留换手率/动量字段位。
 */
const poolRows = computed(() =>
  (poolData.value.pool ?? []).map((rec) => ({
    code: String(rec.symbol ?? '—'),
    turnover: '—',
    momentum: '—',
  })),
)
</script>

<template>
  <!-- 驾驶舱外壳：暗黑底色 + 顶部刷新条 + 滚动主体（与终端不同，本页可纵向滚动） -->
  <div class="dashboard-shell">
    <!-- 顶部工具条：标题 + 刷新按钮 + 加载态 -->
    <header class="dash-header">
      <div class="dash-title">
        <h1>活跃股池驾驶舱</h1>
        <span class="dash-sub">活跃股池（daily 湖前 50）</span>
      </div>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="loading"
        @click="loadDashboard"
      >
        刷新快照
      </el-button>
    </header>

    <!-- 主体：单块面板（活跃股池表） -->
    <main class="dash-grid">
      <section class="cell cell-pool">
        <div class="cell-caption">活跃股池（换手率 / 动量）</div>
        <el-table
          v-if="poolRows.length > 0"
          :data="poolRows"
          size="small"
          stripe
          height="100%"
          style="width: 100%"
        >
          <el-table-column prop="code" label="代码" min-width="100" />
          <el-table-column prop="turnover" label="换手率" width="90" align="right" />
          <el-table-column prop="momentum" label="动量" width="90" align="right" />
        </el-table>
        <TerminalWatermark
          v-else
          compact
          subtitle="活跃股池为空（daily 湖未同步）"
        />
      </section>
    </main>
  </div>
</template>

<style scoped>
/*
 * 驾驶舱外壳：与终端共享极夜黑底色。
 *
 * Why flex:1 + min-height:0 + overflow:auto 而非 min-height:100vh：
 *   App.vue 是「顶部导航(36px) + router-view」纵向 flex 壳，本视图填满除导航
 *   外的剩余高度。面板在窄屏下总高可能超出，故本视图自身允许纵向滚动
 *   （overflow:auto），把滚动限制在驾驶舱内部，不污染整页。
 */
.dashboard-shell {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--qt-bg-page);
  color: var(--qt-text-primary);
  display: flex;
  flex-direction: column;
}

/* 顶部工具条：固定高度，细分隔线分隔主体 */
.dash-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--qt-border);
  background: var(--qt-bg-card);
  flex-shrink: 0;
}

.dash-title h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--qt-text-primary);
}

.dash-sub {
  font-size: 11px;
  color: var(--qt-text-secondary);
  margin-left: 8px;
}

/* 主体 Grid：单块独占整行；min-height 让面板在宽屏下也能撑满 */
.dash-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr;
  grid-template-rows: minmax(280px, 1fr);
  gap: 10px;
  padding: 10px;
}

/* 面板单元格：暗卡片 + 极弱灰边框 + 内边距 + 隐藏溢出 */
.cell {
  background: var(--qt-bg-card);
  border: 1px solid var(--qt-border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cell-caption {
  font-size: 12px;
  color: var(--qt-text-secondary);
  margin-bottom: 8px;
  flex-shrink: 0;
}

.cell-pool { grid-column: 1 / 2; grid-row: 1 / 2; }
</style>
