@echo off
chcp 65001 >nul
cd /d D:\project\clawdata
python -m clawdata.web --port 8000 --host 0.0.0.0
pause
