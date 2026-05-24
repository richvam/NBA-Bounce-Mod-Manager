@echo off
:: Quick launcher - assumes SETUP_AND_RUN.bat was already run once

python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please run SETUP_AND_RUN.bat first.
    pause
    exit /b 1
)

python -c "import UnityPy" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [!] UnityPy not installed. Running setup first...
    echo.
    call "%~dp0SETUP_AND_RUN.bat"
    exit /b
)

python "%~dp0app.py"
