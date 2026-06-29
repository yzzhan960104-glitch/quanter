// ECharts 暗色终端主题：A 股惯例红涨绿跌（candlestick color=红涨 / color0=绿跌）
import { registerTheme } from 'echarts/core'

/**
 * 注册全局 ECharts 暗色终端主题（theme id = 'terminal-dark'）
 *
 * 设计意图（反黑盒）：
 * - 配色取 GitHub Dark 终端调色板（#0d1117 底 / #30363d 边 / #8b949e 次要文本），
 *   与 Element Plus dark css-vars 视觉一致，避免图表区域出现亮色割裂感。
 * - candlestick 严格遵循 A 股视觉惯例：阳线（涨）红 #ef5350、阴线（跌）绿 #26a69a，
 *   切勿按欧美惯例（绿涨红跌）配置，否则研究员会误读信号方向。
 * - 调用时机：main.ts 应用启动时调用一次即可，registerTheme 是幂等覆盖。
 */
export function initTerminalDarkTheme(): void {
  registerTheme('terminal-dark', {
    backgroundColor: '#0d1117',
    textStyle: { color: '#c9d1d9' },
    title: { textStyle: { color: '#e6edf3' }, subtextStyle: { color: '#8b949e' } },
    legend: { textStyle: { color: '#c9d1d9' } },
    tooltip: {
      backgroundColor: 'rgba(22,27,34,0.95)',
      borderColor: '#30363d',
      textStyle: { color: '#c9d1d9' },
    },
    categoryAxis: {
      axisLine: { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e' },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    candlestick: {
      itemStyle: {
        color: '#ef5350',         // 阳线（涨）—— 红
        color0: '#26a69a',        // 阴线（跌）—— 绿
        borderColor: '#ef5350',
        borderColor0: '#26a69a',
      },
    },
    color: ['#58a6ff', '#f78166', '#3fb950', '#d29922', '#bc8cff'],
  })
}
