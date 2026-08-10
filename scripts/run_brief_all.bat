@echo off
chcp 65001 >nul
cd /d "E:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv310\Scripts\python.exe" ops/brief_all.py >> logs\brief_all.log 2>&1
