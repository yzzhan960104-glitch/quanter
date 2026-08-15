@echo off
chcp 65001 >nul
cd /d E:\quanter
set PYTHONUTF8=1
rem T17 (T4 leftover): logs dir guard - cmd '>>' redirect never auto-creates the
rem dir; on a clean checkout the bat would fail silently (no audit log, audit dead).
if not exist logs mkdir logs
".venv310\Scripts\python.exe" "scriptsudit_ssot.py" >> logsudit_schtask.log 2>&1
