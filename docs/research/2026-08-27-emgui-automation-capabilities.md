# 掘金终端（东财 Goldminer3）GUI 自动化能力探索报告

日期：2026-08-27 ｜ 分支：feature/emquant-pilot-0821 ｜ 探索动机：用户裁决掘金将作为主交易平台，需摸清 GUI 自动化能力边界。

## 一、平台技术指纹（已实锤）

| 项 | 结论 | 证据 |
|---|---|---|
| GUI 框架 | **Electron（VS Code 改造壳）** | `resources/electron.asar`、`node_modules.asar`、`package.json` 含 `"vscodeVersion": "1.32.1"`、`out/{main.js,bootstrap*.js,nls.metadata.json}` = VS Code 源码布局 |
| 版本 | 3.22.0.18（productVer） | emgm3.exe VersionInfo，东方财富信息股份有限公司 |
| UI 本质 | **Chromium 渲染的网页** | content_shell.pak / blink_image_resources / icudtl.dat / v8_context_snapshot.bin |
| 单实例锁 | `goldminer` 互斥体（重启才能换启动参数） | product.json `win32MutexName` |
| 深链协议 | `goldminer://` URL Protocol | product.json `urlProtocol` |
| 用户数据 | `~/.emgm3`（Chromium profile：Cache/IndexedDB/Local Storage/extensions） | 目录实测 |

## 二、服务端口图谱（gmterm-serv.exe，Go 系，父进程=emgm3）

配置源 `~/.emgm3/.gmserv.json`：

| 端口 | 用途 | 实测 |
|---|---|---|
| 7001 | SDK RPC（策略进程 serv_addr） | gm SDK 直连，已在用 |
| 7002 | RPC 网关（HTTP） | HTTP 服务，路由私有（常见路径全 404） |
| 7003 | 订阅服务 | 非响应（二进制协议） |
| 7004 | 订阅网关（HTTP，Go 风格 404） | 同 7002，路由私有 |

外部接入点：`61.129.116.97:7201/7202`（deamon entries，公网 RPC）。7002/7004 的私有路由是潜在 API 面，但无文档，逆向成本高、优先级低。

## 三、自动化通道实测矩阵

| 通道 | 状态 | 证据/边界 |
|---|---|---|
| **无障碍树（UIA）** | ❌ 空 | CUA 7 匿名元素；PowerShell UIA 深遍历只有窗格 Pane 无子节点（Electron 默认不建 a11y 树；`--force-renderer-accessibility` 未启用） |
| **CUA 像素点击** | ❌ 封死 | 终端持续重绘（行情/时钟/动画），截图→点击的 live-owner 活性校验永过期；zoom 子帧同判；窗口帧 identity mismatch |
| **CUA 截屏观察** | ✅ 可用 | display 级截图 + 视觉模型读数（布局/状态/数字均可读）——只读不写 |
| **PowerShell 截屏栈** | ✅ **主力观察通道** | `emquant/tools/gm_screenshot.ps1`（CopyFromScreen，双屏任选）+ Read 上 CDN + analyze_image 读数——零 CUA 依赖、纯脚本、可无人值守 |
| **PowerShell 键盘栈** | ✅ 可用（慎用） | `emquant/tools/gm_sendkeys.ps1`（AppActivate+SendKeys）；Ctrl+Shift+I 未见 DevTools（单次样本，或被应用裁剪加速键）；焦点是全局资源，用户正在用电脑时会互相干扰 |
| **进程级控制** | ✅ **已投产** | `relaunch_strategy.ps1`：杀/拉策略进程（参数复刻终端命令行）；终端 UI 对外拉进程**可见**（副屏监控页显示 NECK"运行中"+资金 3890=实时持仓市值）——之前"外拉进程不可见"的假设被证伪 |
| **CDP（Chrome DevTools）** | ⏳ **待实验（最高价值）** | Electron 架构原生支持 `--remote-debugging-port=9222`：重启终端带此开关 → `http://127.0.0.1:9222/json` 列出 UI 页面目标 → WebSocket CDP → DOM 查询+JS 注入+程序化点击——**完全绕过像素活性问题，DOM 级自动化**。前置条件：终端重启（单实例锁）；终端重启会带走 gmterm-serv（父进程链）→ 策略进程失去 SDK → **必须收盘后（15:36 EOD 之后）做** |

## 四、推荐自动化架构（分层）

1. **交易面（L0，已投产）**：不动 GUI——gm SDK 直连（策略进程 + relaunch ps1）。下单/对账/风控全部走这里，GUI 只是观察窗。
2. **观察面（L1，已投产）**：gm_screenshot.ps1 + 视觉模型——晨检/异常时对终端 UI 做无侵入读数（持仓/资金/策略状态）。
3. **控制面（L2，待 CDP 实验解锁）**：终端 UI 上的稀缺操作（账户绑定、终端设置、登录态维护）——CDP 是唯一可靠的程序化路径；解锁后可做"终端操作代理"。
4. **goldminer:// 深链（L3，备选）**：深链可能支持直达页面（打开策略/监控），降低 GUI 导航需求——路由格式待逆向（可从 out/ 的 JS bundle 里 grep）。

## 五、CDP 实验方案（就绪待执行，收盘后）

```
1. 等待 15:36 EOD 完成（after_close 落盘）
2. 停策略：relaunch_strategy.ps1 -KillOnly
3. 关终端（emgm3 全进程树）
4. 带 CDP 开关拉起：& "F:\dcjj\Eastmoney Goldminer3\emgm3.exe" --remote-debugging-port=9222
5. curl http://127.0.0.1:9222/json → 期望列出页面 targets（title 含终端窗口）
6. WebSocket 连 CDP → Runtime.evaluate 查 DOM（document.title / 按钮查询）
7. 若通：重启策略（relaunch ps1），验证 audit INIT + 持仓管理恢复
8. 产出：CDP 通道验证报告 + 可选的终端启动快捷方式（带开关，长期生效）
```

风险与回退：开关失败/无 targets → 直接原样重启终端（无开关）+ relaunch 策略，回到 L0+L1 现状；策略状态在 state.pkl，无损失面。

## 六、遗留观察项

- Ctrl+Shift+I 单次未出 DevTools——不排除焦点被主屏窗口截走（SendKeys 全局焦点问题），CDP 通了以后此路径无价值，不再深究。
- 7002/7004 私有 HTTP 路由：价值低（L0 已覆盖交易面），除非未来需要"不经 SDK 的旁路查询"。
- out/ JS bundle 里可能 grep 出 goldminer:// 路由表与内部 IPC 频道名——CDP 实验通过后再顺手做（JS 注入环境下 console 里直接实验更高效）。

## 结论

**掘金终端作为主交易平台的自动化基座成立**：交易面走 SDK（不依赖 GUI，已验证全链路含成交）、观察面走脚本截屏+视觉（已投产）、控制面等 CDP 实验解锁（架构上确定可行，只差一次收盘后的带开关重启）。GUI 像素点击路径已系统性排除，不必再试。
