@echo off
setlocal EnableExtensions
title Git Commit and Push
cd /d "%~dp0"

echo ============================================================
echo   Git Commit + Push Helper
echo ============================================================

set "GIT_EXE="
for /f "delims=" %%G in ('where git 2^>nul') do (
    set "GIT_EXE=%%G"
    goto :git_found
)

:git_found
if not defined GIT_EXE (
    if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT_EXE=%ProgramFiles%\Git\cmd\git.exe"
)
if not defined GIT_EXE (
    if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" set "GIT_EXE=%ProgramFiles(x86)%\Git\cmd\git.exe"
)
if not defined GIT_EXE (
    echo [ERROR] git was not found. Install Git or add it to PATH.
    pause
    exit /b 1
)

set "REPO_DIR=%CD%"
:find_repo
if exist "%REPO_DIR%\.git" goto :repo_found
for %%I in ("%REPO_DIR%\..") do set "PARENT_DIR=%%~fI"
if /I "%PARENT_DIR%"=="%REPO_DIR%" goto :repo_not_found
set "REPO_DIR=%PARENT_DIR%"
goto :find_repo

:repo_found
cd /d "%REPO_DIR%"
"%GIT_EXE%" rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto :repo_not_found
goto :repo_ok

:repo_not_found
    echo [ERROR] Git repository not found in this folder or parents.
    pause
    exit /b 1

:repo_ok

set "MSG=%*"
if "%~1"=="" (
    set /p MSG=Enter commit message: 
)

if not defined MSG (
    echo [ERROR] Commit message is empty.
    pause
    exit /b 1
)

echo.
echo [1/4] git status --short
"%GIT_EXE%" status --short

echo.
echo [2/4] git add .
"%GIT_EXE%" add .
if errorlevel 1 goto :fail

echo.
echo [3/4] git commit
"%GIT_EXE%" commit -m "%MSG%"
if errorlevel 1 goto :fail

echo.
echo [4/4] git push
"%GIT_EXE%" push
if errorlevel 1 goto :fail

echo.
echo [DONE] Commit and push completed.
pause
exit /b 0

:fail
echo.
echo [ERROR] Command failed. Check output above.
pause
exit /b 1
