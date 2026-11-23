@echo off
chcp 65001>nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% "通用问卷自动填报.py"
if errorlevel 1 pause
