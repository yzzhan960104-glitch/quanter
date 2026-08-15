@echo off
chcp 65001 >nul
cd /d E:\quanter
set PYTHONUTF8=1
".venv310\Scripts\python.exe" "scripts\audit_ssot.py" >> logs\audit_schtask.log 2>&1
