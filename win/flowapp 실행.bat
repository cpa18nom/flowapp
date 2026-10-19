@echo off
chcp 65001 >nul
cd /d "%~dp0"
title flowapp
set PYTHONUTF8=1
:run
"runtime\python.exe" "app\win\launcher.py" %*
if %errorlevel%==42 goto run
