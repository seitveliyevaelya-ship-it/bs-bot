@echo off
chcp 65001 >nul
cd /d "%~dp0"
py bot.py 2>nul || python bot.py
pause
