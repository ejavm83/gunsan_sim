@echo off
chcp 65001 >nul
title Gunsan Simulation Web Dashboard
cd /d "%~dp0"
echo ============================================================
echo   Starting Gunsan Simulation Web Dashboard...
echo   Browser opens at http://localhost:PORT (8501~8525 중 빈 포트)
echo   Press Ctrl+C to stop the server.
echo ============================================================
py -3 -c "import streamlit" 2>nul
if errorlevel 1 (
  echo [INFO] streamlit 미설치 - requirements.txt 설치 중...
  py -3 -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo [ERROR] pip install 실패. 수동: py -3 -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
py -3 "%~dp0run_webapp.py"
pause
