// ECharts 暗色终端主题：A 股惯例红涨绿跌（candlestick color=红涨 / color0=绿跌）
import { registerTheme } from 'echarts/core'

/**
 * 注册全局 ECharts 暗色终端主题（theme id = 'terminal-dark'）
 *
 * 设计意图（反黑盒）：
 * - 配色对齐 TradingView 极夜黑体系（#131722 底 / #2b3139 边 / #787b86 次要文本），
 *   与 Element Plus dark css-vars 覆盖层（styles/terminal.css）保持同源，避免图表区
 *   与表单区出现色阶层级断裂。
 * - candlestick 严格遵循 A 股视觉惯例：阳线（涨）红 #ef5350、阴线（跌）绿 #26a69a，
 *   切勿按欧美惯例（绿涨红跌）配置，否则研究员会误读信号方向。
 * - 调色板首色取 Quant 蓝 #2962ff（与全局 primary 同色），多线图首条线即品牌强调色。
 * - 调用时机：main.ts 应用启动时调用一次即可，registerTheme 是幂等覆盖。
 */
export function initTerminalDarkTheme(): void {
  registerTheme('terminal-dark', {
    backgroundColor: '#131722',
    textStyle: { color: '#d1d4dc' },
    title: { textStyle: { color: '#e6edf3' }, subtextStyle: { color: '#787b86' } },
    legend: { textStyle: { color: '#d1d4dc' } },
    tooltip: {
      backgroundColor: 'rgba(30,34,45,0.95)',
      borderColor: '#2b3139',
      textStyle: { color: '#d1d4dc' },
    },
    categoryAxis: {
      axisLine: { lineStyle: { color: '#2b3139' } },
      axisLabel: { color: '#787b86' },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { lineStyle: { color: '#2b3139' } },
      axisLabel: { color: '#787b86' },
      splitLine: { lineStyle: { color: '#232731' } },
    },
    candlestick: {
      itemStyle: {
        color: '#ef5350',         // 阳线（涨）—— 红（A 股惯例，勿改欧美绿涨）
        color0: '#26a69a',        // 阴线（跌）—— 绿
        borderColor: '#ef5350',
        borderColor0: '#26a69a',
      },
    },
    // 调色板：首色 Quant 蓝（与全局 primary 同源），后续多线沿用终端暖冷搭配
    color: ['#2962ff', '#f78166', '#26a69a', '#d29922', '#bc8cff'],
  })
}
