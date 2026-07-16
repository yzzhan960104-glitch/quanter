# 每日行情播报 · Windows 定时触发（schtasks）

> 每日 19:00 自动跑 `python -m broadcast`。**前置**：Task 0 的 robot-code 已写 `.env`。
> 实测于 2026-07-16（分支 `feat/daily-market-brief` Task 4）。

## 注册（每日 19:00 触发）

推荐用 `run_broadcast.bat` 包装（解决 schtasks 默认 cwd=System32 的坑，见下）：

```bat
schtasks /Create /SC DAILY /TN "QuanterDailyBrief" /TR "C:\Users\yzzhan\Desktop\quanter\scripts\run_broadcast.bat" /ST 19:00
```

## 查询 / 立即跑一次 / 删除

```bat
schtasks /Query /TN "QuanterDailyBrief" /V /FO LIST     :: 查下次运行时间/上次结果
schtasks /Run   /TN "QuanterDailyBrief"                  :: 立即手动跑一次
schtasks /Delete /TN "QuanterDailyBrief" /F              :: 卸载定时
```

## cwd 坑（schtasks 经典问题）

`schtasks /TR` 默认 cwd 是 `%WINDIR%\System32`，而 `python -m broadcast` 依赖项目根的
`config/` `data_lake/` `broadcast/`。直接 `/TR "python.exe -m broadcast"` 会报
`ModuleNotFoundError` / 找不到 data_lake。

**解法**：`scripts/run_broadcast.bat` 先 `cd /d` 到项目根再跑（已提供）：
```bat
@echo off
cd /d C:\Users\yzzhan\Desktop\quanter
"C:\Users\yzzhan\Desktop\quanter\.venv310\Scripts\python.exe" -m broadcast >> logs\broadcast.log 2>&1
```

## 备注

- **幂等**：broadcast 内置 `logs/.last_broadcast` 去重；周末/节假日 `index_daily` 不更新 → 自动跳过零废报。
- **失败重试**：推送失败不写 `last_broadcast`，下次 19:00 自动重试（不丢）。
- **凭证**：`.env` 的 `DINGTALK_CHAT_ROBOT_CODE` + `BROADCAST_GROUP_ID`（Task 0 产出）。
- **日志**：`logs/broadcast.log`（bat 重定向 stdout/stderr）+ Python logger。
- **无常驻进程**：schtasks 触发即跑、跑完即退，不依赖 uvicorn / dws dev connect 常驻。
