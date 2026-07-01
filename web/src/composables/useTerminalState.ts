/**
 * 终端全局状态（模块级 reactive 单例，替代 Pinia）
 *
 * Epic 4 改造核心：废弃阻塞式 axios.post，改用原生 EventSource 接收 per-run 流。
 *
 * 为何改 SSE（流式）而不是继续用阻塞式 HTTP：
 * - 阻塞式 axios.post 把整个回测计算压在一个请求里，长周期回测（数千根 K 线 +
 *   HMM 拟合）极易触发 502/网关超时，且浏览器无任何中间反馈。
 * - 改两段式后：createBacktestRun 拿 run_id（毫秒级）→ 开 EventSource
 *   接收 progress/trade/risk/result 帧，前端边收边渲染买卖点与风控告警。
 * - EventSource 是浏览器原生 API，自动断线重连；但 per-run 流语义上是一次性的，
 *   这里 [DONE] / onerror 都会主动 close，避免重连把已结束的 run 拉成僵尸流。
 *
 * 事件归一化：
 * - progress/trade/risk 帧 → LogEntry（终端按级别高亮：trade=SUCCESS / risk=WARNING|ERROR / progress=INFO）
 * - result 帧 → state.result（markRaw 阻止深度代理海量时序，触发 ProChart/NavChart 渲染）
 * - error 帧 / onerror → state.error
 * - [DONE] → 关流，loading=false
 *
 * result 仍用 markRaw 的原因（沿用旧设计）：
 * - 回测响应含数百~数千节点的 nav_series / ohlcv / trades，深度响应式代理开销极大。
 * - markRaw 阻止 reactive 递归代理 result 内部，仅追踪 result 引用本身的替换。
 *   海量只读时序数据不应被深度代理（参见 Vue 官方关于大只读集合的性能建议）。
 */
import { reactive, toRefs, markRaw, ref } from 'vue'
import {
  createBacktestRun,
  type SingleBacktestParams,
  type SingleBacktestResponse,
} from '@/api/backtest'

/** 终端日志条目（与后端 SSE 事件归一化对齐；TerminalLogs.vue 直接消费） */
export interface LogEntry {
  ts: number
  level: string         // INFO / SUCCESS / WARNING / ERROR
  logger: string        // 'backtest' / 'trade' / 'risk' / 'progress'
  message: string
}

interface TerminalState {
  /** 回测执行中（ParamForm 提交按钮 loading） */
  loading: boolean
  /** 最近一次回测响应；result 帧到达后写入；首次提交前为 null（ProChart 用 v-if 兜底空态） */
  result: SingleBacktestResponse | null
  /** 执行错误信息（HTTP 错误已由拦截器 ElMessage 弹窗，这里仅留文本供右栏红字兜底） */
  error: string
}

// 模块级单例：整个 App 生命周期共享一份状态（同一份 reactive 对象）
const state = reactive<TerminalState>({
  loading: false,
  result: null,
  error: '',
})

/**
 * 日志流（独立 ref，与 state 解耦）。
 *
 * 为何 logs 单独拿 ref 而不进 state：
 * - state 是 reactive（深度代理），但 logs 是高频 append 的大数组（上限 2000），
 *   走 toRefs 拆出来后仍由同一个 reactive 代理——理论上 OK，但为了让
 *   TerminalLogs.vue 直接 watch(logs.value.length) 触发滚动，显式 ref 更直白。
 * - 同时 result 用 markRaw，logs 不应跟着被 markRaw（否则 push 不触发响应），
 *   独立 ref 避免与 result 混在同一容器被误处理。
 */
const logs = ref<LogEntry[]>([])

/** 当前活跃的 EventSource（开新流前先 close 旧流，防止并发 run 串扰） */
let currentES: EventSource | null = null

/**
 * 把后端 SSE 事件 dict 归一化为终端 LogEntry。
 *
 * 后端帧字段（与 server T14 对齐）：
 * - progress：{ type:'progress', date, nav, i, n }
 * - trade   ：{ type:'trade', date, direction, symbol, shares, price }
 * - risk    ：{ type:'risk', level, reason, date, shares?, price?, symbol? }
 * 未知类型兜底为 INFO + JSON 序列化，确保任何坏帧都不会让前端崩。
 *
 * ts 取前端本地时间（Date.now()/1000）：SSE 帧不带 ts，避免与后端时区对齐问题。
 */
function toLogEntry(ev: any): LogEntry {
  const now = Date.now() / 1000
  // 防御性：ev.price / ev.nav 可能是 undefined / 字符串 / NaN，先做安全格式化
  const fmtNum = (v: any) => (typeof v === 'number' && isFinite(v) ? v.toFixed(2) : v)
  switch (ev.type) {
    case 'trade':
      // 成交事件用 SUCCESS（绿）突出，与终端买卖点高亮语义一致
      return {
        ts: now,
        level: 'SUCCESS',
        logger: 'trade',
        message: `${ev.direction} ${ev.symbol} ${ev.shares}@${fmtNum(ev.price)} @ ${ev.date}`,
      }
    case 'risk': {
      // 风控告警级别精细化（Task 18）：按 reason 字段区分止损/止盈/其它。
      //
      // Why 不再单纯依赖后端 level：后端 _close() 发射的分钟级风控平仓 risk 帧统一
      // 标 level="WARN"（见 backtest/engine.py 的 _close），但 reason 可能是
      // "触及止损"/"触及止盈"/"移动止损"——这三者在交易语义上颜色相反（止损=亏损
      // 出场 应红/ERROR，止盈=盈利出场 应绿/SUCCESS），仅按 level 全标黄会掩盖
      // 关键盈亏信号。这里按 reason 文本细化：
      //   - 触及止损 / 移动止损 → lv-error（红，亏损被动出场）
      //   - 触及止盈            → lv-success（绿，盈利被动出场）
      //   - 其它（涨跌停/资金不足/未知）→ 维持后端 level（WARN→WARNING，ERROR→ERROR）
      //
      // 字段取 ev.reason（与后端 backtest/engine.py 的 risk 帧 reason 字段对齐，
      // 后端 reason 形如 "涨停无法买入"/"资金不足"/"跌停无法卖出"/"触及止损"；
      // 此前误取 ev.msg 会得到 undefined，终端风控告警显示为 "undefined @ 日期"）。
      const reason: string = typeof ev.reason === 'string' ? ev.reason : ''
      let level: string
      if (reason.includes('止损')) {
        // 含「止损」字样（"触及止损"/"移动止损"）—— 亏损被动出场，红色 ERROR
        level = 'ERROR'
      } else if (reason.includes('止盈')) {
        // 含「止盈」字样（"触及止盈"）—— 盈利被动出场，绿色 SUCCESS
        level = 'SUCCESS'
      } else {
        // 其它风控（涨跌停/资金不足/断线告警等）—— 维持后端 level，非法值兜底 WARNING
        level = ev.level === 'ERROR' ? 'ERROR' : 'WARNING'
      }
      return {
        ts: now,
        level,
        logger: 'risk',
        message: `${ev.reason} @ ${ev.date}`,
      }
    }
    case 'progress':
      // 进度行：日期 + 当前净值 + (i+1)/n，便于目测回测推进速度
      return {
        ts: now,
        level: 'INFO',
        logger: 'progress',
        message: `${ev.date}  nav=${fmtNum(ev.nav)}  (${ev.i + 1}/${ev.n})`,
      }
    default:
      // 未知帧兜底：完整序列化便于排查，不抛错不丢帧
      return { ts: now, level: 'INFO', logger: 'backtest', message: JSON.stringify(ev) }
  }
}

export function useTerminalState() {
  /**
   * 执行单资产回测（Epic 4：建 run → 开 SSE 流）。
   *
   * 入参 req 即 ParamForm single 分支 emit 的 payload，字段形状与
   * SingleBacktestParams 对齐（symbol/start_date/end_date/initial_capital/
   * signal_freq/strategy_name/strategy_params）——原样透传，无需字段映射。
   *
   * 流程：
   * 1) 先关旧流（currentES?.close()）：避免用户连点提交导致多个 EventSource 并发，
   *    旧 run 的帧污染新 run 的终端（典型场景：用户改参数重跑）。
   * 2) createBacktestRun 拿 run_id（毫秒级）；建 run 失败由拦截器 ElMessage 提示。
   * 3) 开 EventSource；onmessage 按 type 分流到 result / error / logs。
   * 4) [DONE] → close 流 + loading=false。
   * 5) onerror（流中断/服务端关连）→ 设 error + close，避免 loading 卡死。
   */
  async function execute(req: SingleBacktestParams) {
    state.loading = true
    state.error = ''
    // 每次新 run 清空旧日志，避免上一轮回测的买卖点残留误导用户
    logs.value = []
    // 关旧流：防止并发 run 的 SSE 帧相互串扰
    currentES?.close()
    currentES = null

    try {
      const { run_id } = await createBacktestRun(req)
      const es = new EventSource(`/api/v1/backtest/run/stream/${run_id}`)
      currentES = es

      es.onmessage = (e) => {
        // [DONE]：后端约定流结束标记，关闭 EventSource 并解除 loading
        if (e.data === '[DONE]') {
          es.close()
          currentES = null
          state.loading = false
          return
        }
        // 解析 JSON 帧；坏帧静默丢弃（不污染终端，也不抛错中断流）
        let ev: any
        try {
          ev = JSON.parse(e.data)
        } catch {
          return
        }
        if (ev.type === 'result') {
          // result 帧承载 SingleBacktestResponse（metrics/nav_series/ohlcv/trades/...）
          // markRaw：阻止 reactive 深度代理海量时序，仅追踪引用替换触发刷新
          state.result = markRaw(ev.data as SingleBacktestResponse)
          // 收到 result 即视为回测实质完成（[DONE] 紧随其后），提前解除 loading
          state.loading = false
        } else if (ev.type === 'error') {
          // 后端显式 error 帧：回测过程抛异常（如数据缺失、参数非法）
          state.error = ev.message || '回测执行失败'
          state.loading = false
        } else {
          // progress / trade / risk → 归一化为日志条目
          const entry = toLogEntry(ev)
          logs.value.push(entry)
          // 防爆内存：仅保留最近 2000 条（与 TerminalLogs 上限一致）
          if (logs.value.length > 2000) {
            logs.value.splice(0, logs.value.length - 2000)
          }
        }
      }

      es.onerror = () => {
        // 流中断（网络抖动 / 服务端崩 / 中途关连）：关流 + 设 error，避免 loading 卡死。
        // 不依赖浏览器自动重连——per-run 流语义是一次性的，重连可能拉到已结束 run 的空流。
        state.error = state.error || '日志流中断'
        es.close()
        currentES = null
        state.loading = false
      }
    } catch (e: any) {
      // createBacktestRun 失败（HTTP 4xx/5xx/超时）：拦截器已 ElMessage 提示
      state.error = e?.message || '创建回测失败'
      // 失败时清空旧响应，防止面板展示与新参数不一致的过期数据
      state.result = null
      state.loading = false
    }
  }

  // toRefs：把 state 字段拆成独立 ref 返回，便于调用方解构后保持响应性
  // logs 单独返回（已在模块级定义，所有调用方共享同一份引用）
  return { ...toRefs(state), logs, execute }
}
