@echo off
chcp 65001 >nul
cd /d D:\project\clawdata
python -m clawdata --hot-list-only --logfile D:\project\clawdata\logs\nightly.log
