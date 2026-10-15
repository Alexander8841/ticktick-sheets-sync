@echo off
chcp 65001 >nul
title Синхронизация занятий (TickTick в Google Sheets)

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%"

if exist "C:\Users\alexander\.conda\envs\ml_base\python.exe" (
    set "PYTHON_EXE=C:\Users\alexander\.conda\envs\ml_base\python.exe"
) else (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "%SCRIPT_DIR%src\sync.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo 📄 Подробный журнал работы: "%SCRIPT_DIR%logs\sync.log"

popd

for %%A in (%*) do (
    if /i "%%~A"=="--no-pause" exit /b %EXIT_CODE%
    if /i "%%~A"=="--non-interactive" exit /b %EXIT_CODE%
)

if "%~1"=="--no-pause" exit /b %EXIT_CODE%
if "%~1"=="--non-interactive" exit /b %EXIT_CODE%

echo.
pause
exit /b %EXIT_CODE%
