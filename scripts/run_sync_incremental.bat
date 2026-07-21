@echo off
REM Tushare 数据湖日频增量同步 schtasks 触发入口（Task #11 债 G）
REM Why .bat 包装：schtasks 默认 cwd=%WINDIR%\System32，直接跑 python scripts/sync_incremental.py
REM 会因找不到 config/ data_lake/ data/ 而 ModuleNotFoundError。先 cd /d 项目根再跑。
REM 日志重定向到 data_lake/.syncing/sync_incremental.stdout.log（脚本内部也会追加写到
REM sync_incremental.log，此处的 stdout 捕获异常 traceback 与控制台总结，方便排障）。

cd /d C:\Users\yzzhan\Desktop\quanter
"C:\Users\yzzhan\Desktop\quanter\.venv310\Scripts\python.exe" scripts/sync_incremental.py >> data_lake/.syncing/sync_incremental.stdout.log 2>&1
