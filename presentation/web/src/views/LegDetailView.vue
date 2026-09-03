<!--
  LegDetailView 腿详情页（2026-09-01 用户需求②③）。

  物理意图：点击腿后的"全量策略信息"页——身份（策略名/id/build_stamp/账户锁/
  universe 规模）+ 手动风控参数（pos_cap/CAP.txt 人工总仓位/单日新挂闸/amihud
  keep-top/RISK_BLOCK 旗态）+ 策略参数（识别/执行/交易配置三组常量，部署产物
  §0 = 运行真相）+ 该腿持仓/当日流水/事件流（复用 cockpit 组件，cockpit-leg
  provide 随路由参数）。数据=leg_detail_<leg>.json 静态档案。
-->
<template>
  <div class="leg-detail">
    <div class="head">
      <h2>{{ detail?.leg?.label || legKey }} <span class="sub">{{ roleLabel }}</span></h2>
      <router-link to="/cockpit" class="back">← 返回综合看板</router-link>
    </div>

    <el-row :gutter="12">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>策略身份</template>
          <div class="kv"><span>策略名</span><b>{{ detail?.leg?.strategy_name || '—' }}</b></div>
          <div class="kv"><span>策略 ID</span><b class="mono">{{ detail?.leg?.strategy_id || '—' }}</b></div>
          <div class="kv"><span>构建版本</span><b class="mono small">{{ detail?.build_stamp || detail?.artifact_error || '—' }}</b></div>
          <div class="kv"><span>账户锁</span><b class="mono">{{ detail?.leg?.account_id || '—' }}</b></div>
          <div class="kv"><span>股票池</span><b>{{ detail?.universe_size ?? '—' }} 只（创/科）</b></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>手动风控参数</template>
          <div class="kv"><span>单票仓位上限</span>
            <b>{{ pct(tradeCfg.pos_cap) }}</b></div>
          <div class="kv"><span>人工总仓位上限</span>
            <b>{{ pct(detail?.risk?.cap_total, 1.0) }}</b></div>
          <div class="kv"><span>单日新挂上限</span>
            <b>{{ detail?.daily_order_cap ?? '—' }} 单/日</b></div>
          <div class="kv"><span>信号过滤</span>
            <b>amihud keep-top {{ detail?.amihud_filter?.keep_top ?? '—' }}</b></div>
          <div class="kv"><span>增量挂单闸</span>
            <b :class="detail?.risk?.risk_block ? 'blocked' : 'ok'">
              {{ detail?.risk?.risk_block ? 'RISK_BLOCK 拦截中' : '放行（无人工旗）' }}
            </b></div>
          <div class="kv"><span>最小申报门槛</span>
            <b>科创板 ≥200 股 / 其余 ≥100 股</b></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>识别参数（ID_PARAMS）</template>
          <div class="params">
            <div v-for="(v, k) in idParams" :key="k" class="kv">
              <span class="mono">{{ k }}</span><b>{{ v }}</b>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>执行/风控参数（EXEC_PARAMS · TRADE_CFG）</template>
          <div class="params">
            <div v-for="(v, k) in execParams" :key="k" class="kv">
              <span class="mono">{{ k }}</span><b>{{ v }}</b>
            </div>
            <div v-for="(v, k) in tradeCfg" :key="`t-${k}`" class="kv">
              <span class="mono">cfg.{{ k }}</span><b>{{ v }}</b>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row style="margin-top: 12px">
      <el-col :span="24"><VersionHistoryCard /></el-col>
    </el-row>
    <el-row style="margin-top: 12px">
      <el-col :span="24"><LoserReviewCard :leg="legKey" /></el-col>
    </el-row>
    <el-row style="margin-top: 12px">
      <el-col :span="24"><PositionsPanel /></el-col>
    </el-row>
    <el-row style="margin-top: 12px">
      <el-col :span="24"><TradesTable /></el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, provide, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getLegDetail, type LegDetail } from '../api/home'
import PositionsPanel from '../components/cockpit/PositionsPanel.vue'
import TradesTable from '../components/cockpit/TradesTable.vue'
import VersionHistoryCard from '../components/experiment/VersionHistoryCard.vue'

const route = useRoute()
const legKey = computed(() => String(route.params.leg || 'main'))
const detail = ref<LegDetail | null>(null)

/* 腿全域：PositionsPanel/TradesTable inject 跟随路由参数（本页即选腿器）。 */
const currentLeg = ref(legKey.value)
provide('cockpit-leg', currentLeg)
watch(legKey, (v) => { currentLeg.value = v; load() })

async function load() {
  detail.value = await getLegDetail(legKey.value).catch(() => null)
}
onMounted(load)

const roleLabel = computed(() =>
  detail.value?.leg?.role === 'challenger' ? '挑战者 · R10 候选对照' : '在位者 · R6-8 冠军')
const idParams = computed(() => detail.value?.id_params ?? {})
const execParams = computed(() => detail.value?.exec_params ?? {})
const tradeCfg = computed(() => detail.value?.trade_cfg ?? {})
const pct = (v: unknown, dflt = 0) =>
  (typeof v === 'number' && Number.isFinite(v)) ? `${(v * 100).toFixed(1)}%`
  : (dflt ? `${(dflt * 100).toFixed(0)}%` : '—')
</script>

<style scoped>
.leg-detail { flex: 1; overflow-y: auto; width: 100%; padding: var(--qt-space-3, 12px); max-width: 1200px; margin: 0 auto; }
.head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; }
.head h2 { margin: 0; }
.sub { font-size: 13px; font-weight: 400; color: var(--el-text-color-secondary); }
.back { color: var(--el-text-color-secondary); font-size: 13px; text-decoration: none; }
.kv { display: flex; justify-content: space-between; gap: 12px; padding: 5px 0;
      border-bottom: 1px dashed var(--qt-border, #dcdfe6); font-size: 13px; }
.kv:last-child { border-bottom: none; }
.kv span { color: var(--el-text-color-secondary); }
.mono { font-family: var(--qt-font-mono, monospace); }
.small { font-size: 12px; font-weight: 400; }
.params { max-height: 320px; overflow-y: auto; }
.ok { color: var(--el-color-success); }
.blocked { color: var(--el-color-danger); font-weight: 600; }
</style>
