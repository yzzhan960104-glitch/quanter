@echo off
REM 每日行情播报 schtasks 触发入口（feat/daily-market-brief Task4）
REM Why .bat 包装：schtasks 默认 cwd=%WINDIR%\System32，直接跑 python -m broadcast 会因
REM 找不到 config/ data_lake/ broadcast/ 而 ModuleNotFoundError。先 cd /d 项目根再跑。
REM 日志重定向到 logs/broadcast.log（与 logs/.last_broadcast 同目录）。

cd /d C:\Users\yzzhan\Desktop\quanter
"C:\Users\yzzhan\Desktop\quanter\.venv310\Scripts\python.exe" -m broadcast >> logs\broadcast.log 2>&1
