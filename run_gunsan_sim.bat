@echo off
setlocal EnableExtensions
title Gunsan Hybrid Simulation Runner

cd /d "%~dp0"

echo ============================================================
echo   Gunsan Hybrid Simulation Runner
echo ============================================================
echo.
echo [1] Default run (7 days + charts + CP-SAT)
echo [2] Default + animation GIF
echo [3] Default + HTML report
echo [4] Default + animation + report
echo [5] Custom days + animation + report
echo [0] Exit
echo.
set /p MENU=Select option 0-5: 

if "%MENU%"=="0" goto :done_quick
if "%MENU%"=="1" goto :run_default
if "%MENU%"=="2" goto :run_animate
if "%MENU%"=="3" goto :run_report
if "%MENU%"=="4" goto :run_all
if "%MENU%"=="5" goto :run_custom

echo.
echo [ERROR] Invalid option.
goto :end

:run_default
echo.
echo [RUN] py -3 main.py --days 7
py -3 main.py --days 7
goto :end

:run_animate
echo.
echo [RUN] py -3 main.py --days 7 --animate out/factory.gif
py -3 main.py --days 7 --animate out/factory.gif
goto :end

:run_report
echo.
echo [RUN] py -3 main.py --days 7 --report out/report.html
py -3 main.py --days 7 --report out/report.html
goto :end

:run_all
echo.
echo [RUN] py -3 main.py --days 7 --animate out/factory.gif --report out/report.html
py -3 main.py --days 7 --animate out/factory.gif --report out/report.html
goto :end

:run_custom
echo.
set /p DAYS=Enter simulation days (ex: 14): 
if "%DAYS%"=="" set DAYS=7
echo.
echo [RUN] py -3 main.py --days %DAYS% --animate out/factory_%DAYS%d.gif --report out/report_%DAYS%d.html
py -3 main.py --days %DAYS% --animate out/factory_%DAYS%d.gif --report out/report_%DAYS%d.html
goto :end

:end
echo.
echo ============================================================
echo   Finished. Press any key to close.
echo ============================================================
pause >nul
goto :done

:done_quick
echo Exiting.

:done
endlocal
