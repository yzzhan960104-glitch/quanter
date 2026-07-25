@echo off
REM discovery daemon 夜跑入口（spec §5.2/§10，Plan 4 Task 4）
REM schtasks 触发本 bat：激活 venv → cd 项目根 → python -m discovery daemon --budget 4h
REM Why cd /d %~dp0\..：%~dp0 是 bat 所在目录（discovery/），上一级即项目根，
REM    保证无论 schtasks 以哪个 cwd 启动都能定位 .venv310 与 discovery 包。
cd /d %~dp0\..
call .venv310\Scripts\activate.bat
python -m discovery daemon --budget 4h
