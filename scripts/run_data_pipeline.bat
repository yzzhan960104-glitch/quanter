@echo off
chcp 65001 >nul
cd /d "F:\quanter"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv310\Scripts\python.exe" ops/data_pipeline.py >> logs\data_pipeline.log 2>&1
