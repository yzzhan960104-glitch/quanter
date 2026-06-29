/**
 * 终端全局状态（模块级 reactive 单例，替代 Pinia）
 *
 * 设计意图（反黑盒 / 极简）：
 * - 不引入 Pinia 这种"黑盒状态库"。直接把一个 `reactive` 对象挂在模块作用域，
 *   所有调用 `useTerminalState()` 的组件拿到的是同一份引用——Vue 官方推荐的轻量单例模式。
 * - App.vue 触发 `execute(req)`，各面板（ProChart / MetricCards / PositionsTable）
 *   通过 `result` 读取同一份响应，自动响应式刷新。
 *
 * 为什么 result 用 `shallow` 语义：
 * - 回测响应含数百~数千节点的 nav_series / ohlcv / trades，深度响应式代理开销极大
 *   （参见 SingleBacktest.vue 原注释：750+ NavPoint 各自生成 Proxy）。
 * - 这里用 `markRaw` 阻止 reactive 递归代理 result 内部，仅追踪 result 引用本身的替换。
 *   海量只读时序数据不应被深度代理。
 */
import { reactive, toRefs, markRaw } from 'vue'
import {
  runSingleBacktest,
  type SingleBacktestParams,
  type SingleBacktestResponse,
} from '@/api/backtest'

interface TerminalState {
  /** 回测执行中（ParamForm 提交按钮 loading） */
  loading: boolean
  /** 最近一次回测响应；首次提交前为 null（ProChart 用 v-if 兜底空态） */
  result: SingleBacktestResponse | null
  /** 执行错误信息（Axios 拦截器已 ElMessage 提示，这里仅留文本供右栏红字兜底） */
  error: string
}

// 模块级单例：整个 App 生命周期共享一份状态
const state = reactive<TerminalState>({
  loading: false,
  result: null,
  error: '',
})

export function useTerminalState() {
  /**
   * 执行单资产回测。
   *
   * 入参 req 即 ParamForm single 分支 emit 的 payload，字段形状与
   * `SingleBacktestParams` 对齐（symbol/start_date/end_date/initial_capital/
   * signal_freq/strategy_name/strategy_params）——无需任何字段映射，原样透传。
   *
   * 错误处理：HTTP 错误已由 api/backtest.ts 的响应拦截器统一 ElMessage 弹窗，
   * 此处只把 message 写入 state.error 供右栏兜底显示，并清空旧 result 避免脏数据残留。
   */
  async function execute(req: SingleBacktestParams) {
    state.loading = true
    state.error = ''
    try {
      const res = await runSingleBacktest(req)
      // markRaw：阻止 reactive 递归代理海量时序数据，仅追踪引用替换
      state.result = markRaw(res)
    } catch (e: any) {
      state.error = e?.message || '回测执行失败'
      // 失败时清空旧响应，防止面板展示与新参数不一致的过期数据
      state.result = null
    } finally {
      state.loading = false
    }
  }

  // toRefs：把 state 的字段拆成独立 ref 返回，便于调用方解构后保持响应性
  return { ...toRefs(state), execute }
}
