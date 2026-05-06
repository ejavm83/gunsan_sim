@echo off
title Gunsan Simulation Web Dashboard
cd /d "%~dp0"
echo ============================================================
echo   Starting Gunsan Simulation Web Dashboard...
echo   Browser will open automatically at http://localhost:8501
echo   Press Ctrl+C to stop the server.
echo ============================================================
py -3 -m streamlit run webapp.py --server.port 8501
pause
