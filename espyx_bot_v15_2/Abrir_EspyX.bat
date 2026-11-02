@echo off
title EspyX Bot
cd /d "%~dp0"
set PY=python
%PY% --version >nul 2>&1 || set PY=py
%PY% --version >nul 2>&1 || (
  echo No encontre Python. Instalalo desde https://www.python.org/downloads/ marcando "Add Python to PATH".
  pause
  exit /b
)
%PY% -c "import requests, websocket" >nul 2>&1 || %PY% -m pip install requests websocket-client==1.8.0
%PY% app.py
if errorlevel 1 pause
