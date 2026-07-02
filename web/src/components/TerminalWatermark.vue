<script setup lang="ts">
/**
 * 极简空态水印（Epic 3：抹除廉价感）
 *
 * 设计意图（反黑盒）：
 * - 替换 Element Plus 默认 <el-empty> 的「纸箱子」插画。量化终端的空态不该是
 *   「无数据」的歉意，而是「等待指令」的克制留白——故用极低透明度的巨型
 *   品牌水印 + 一行说明小字，把空区域当成终端的「待激活画布」。
 * - 纯展示、零依赖、零 props 状态：仅靠 CSS 控制层级，不引入图表/图标库。
 *
 * Why 不用图片/SVG：水印本质是文字质感，用 font-weight + letter-spacing +
 *   透明度即可表达「终端离线感」，避免引入额外静态资源与 HTTP 请求。
 */
withDefaults(
  defineProps<{
    /** 巨型主水印文字（默认品牌名） */
    title?: string
    /** 下方说明小字（场景化文案，由调用方传入） */
    subtitle?: string
    /** 紧凑模式：面板高度有限时缩小字号，避免水印撑爆容器 */
    compact?: boolean
  }>(),
  {
    title: 'Quanter Terminal',
    subtitle: 'Waiting for simulation parameters...',
    compact: false,
  },
)
</script>

<template>
  <div class="terminal-watermark" :class="{ compact }">
    <!-- 巨型主水印：极低透明度，作为面板背景质感而非抢眼内容 -->
    <div class="wm-title">{{ title }}</div>
    <!-- 说明小字：稍高透明度，告诉用户「下一步做什么」而非「这里空了」 -->
    <div class="wm-subtitle">{{ subtitle }}</div>
  </div>
</template>

<style scoped>
/* 撑满父容器并水平垂直居中；父容器需有确定高度（Grid/flex 单元格已满足） */
.terminal-watermark {
  width: 100%;
  height: 100%;
  min-height: 120px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  user-select: none;            /* 水印是装饰，不允许选中复制，避免干扰数据交互 */
  pointer-events: none;         /* 不拦截点击，便于父级未来叠加按钮/重试入口 */
}

/* 巨型主水印：TradingView 极夜黑上的幽蓝幽光字，透明度压到 ~5% 只作肌理 */
.wm-title {
  font-size: 30px;
  font-weight: 800;
  letter-spacing: 3px;
  color: #d1d4dc;
  opacity: 0.05;
  white-space: nowrap;
}

/* 说明小字：次要文字色 + 中等透明度，可读但不喧宾夺主 */
.wm-subtitle {
  font-size: 12px;
  letter-spacing: 0.5px;
  color: #787b86;
  opacity: 0.55;
  font-variant-numeric: tabular-nums;
}

/* 紧凑模式：小面板（如右栏指标下方的局部空位）缩小水印，防止溢出 */
.terminal-watermark.compact .wm-title {
  font-size: 18px;
  letter-spacing: 2px;
}
.terminal-watermark.compact .wm-subtitle {
  font-size: 11px;
}
.terminal-watermark.compact {
  min-height: 60px;
}
</style>
