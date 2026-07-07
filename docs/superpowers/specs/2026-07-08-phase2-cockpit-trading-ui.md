# Phase 2：前端 Cockpit 交易 UI 设计

> 日期：2026-07-08
> 背景：EMT 接入完成（Phase 1.5），前端 `LiveCockpitView` 加连接/下单/撤单/订单/资产 UI
> 范围：`web/src/api/trading.ts` +6 方法 + `LiveCockpitView.vue` UI 扩展
> 券商：**EMT**（后端 `get_gateway` 按 env 选；前端不感知券商，调 HTTP 即可）

---

## 1. 现状（LiveCockpitView 已有，不重写）

- 心跳四态灯（2s 轮询 `/status`，严格镜像后端 mode）
- 紧急熔断按钮（el-popconfirm → `/emergency_halt`）
- 持仓 Treemap（面积=市值，红涨绿跌）+ 持仓明细表
- CSV 导出（`/export`）
- 路由 `/live` 已挂载（router/index.ts）

## 2. 新增（Phase 2 交付）

### 2.1 `api/trading.ts` +6 方法 + 类型

```ts
connect(): Promise<{connected: boolean; mode: string}>
disconnect(): Promise<{connected: boolean}>
submitOrder(body: SubmitOrderBody): Promise<{order_id: string; state: string; message: string}>
cancelOrder(orderId: string): Promise<{order_id: string; state: string; message: string}>
getOrders(): Promise<{orders: OrderRow[]}>
getAsset(): Promise<{asset: Asset}>

interface SubmitOrderBody {
  symbol: string; qty: number; side: 'buy'|'sell';
  price: number | null; dry_run: boolean; confirm: boolean
}
interface OrderRow {
  kind?: string; order_emt_id?: string|number; order_id?: string|number;
  ticker?: string; order_status?: number; state: string;
  qty_traded?: number; qty_left?: number; price?: number; side?: number;
  error_msg?: string
}
interface Asset { account_id?: string; cash: number; total_asset: number; market_value: number }
```

### 2.2 `LiveCockpitView.vue` UI 扩展（既有四块保留）

| 区块 | 位置 | 行为 |
|---|---|---|
| **连接/断开按钮** | 心跳灯右侧 | `disconnected`→显示「连接」（调 `/connect`）；`live`→显示「断开」（调 `/disconnect`）；连接中 loading |
| **资产卡** | 工具条新增 | 总资产 / 可用资金（`live` 态 5s 轮询 `/asset`；非 live 清空） |
| **下单面板** | 持仓表上方，el-form | symbol / qty / side(buy/sell) / price / **dry_run el-switch**（默认开=模拟）/ confirm；提交调 `/submit_order` |
| **订单列表** | 下单面板下方 | `/orders` 3s 轮询；列：标的/方向/数量/价格/状态/撤单按钮（仅 SUBMITTED/PARTIAL_FILLED 可撤） |

### 2.3 dry_run 双开关（前端核心，spec §6.1 已定）

- 前端 `dry_run=true`（默认）→ 模拟（不真下单，落 DRY_RUN 流水，返回 `state=DRY_RUN`）
- 前端 `dry_run=false` → 后端 `risk_shield` 10 关 + env `QMT_ALLOW_LIVE_TRADE` 总闸
- 前端 dry_run 开关旁显眼标注当前模式（模拟/实盘），防误触

## 3. 轮询纪律（防虚假繁荣）

- status：2s（既有）
- orders：`live` 态 3s；非 live 停止 + 清空
- asset：`live` 态 5s；非 live 清空
- 所有定时器 `onBeforeUnmount` 清理

## 4. 错误处理

- `/connect` 503（网关未装配/login 失败）→ ElMessage.error + 友好提示
- `/submit_order` 409（挡板命中）→ ElMessage.warning + reason
- dry_run 成功（state=DRY_RUN）→ ElMessage.info「模拟下单已记录」
- 真单成功（state=SUBMITTED）→ ElMessage.success + order_id

## 5. 测试与验收

- `npm run build`（前端 TS 编译无错）
- 端到端：`.venv310` 启后端 + 浏览器开 `/live`，验证：
  - 点「连接」→ status 变 live + 资产/持仓显示
  - dry_run 下单 → DRY_RUN 提示
  - 关 dry_run + 白名单内 100 股 → 真单 + 订单列表 + 撤单
- 无前端单测基建（项目前端无 vitest），靠 build + 端到端验收

## 6. 非目标

- 前端单测框架引入（YAGNI）
- 行情 K 线图（Phase 3 或单独 epic）
- 策略引擎自动实盘 UI（Phase 3）
