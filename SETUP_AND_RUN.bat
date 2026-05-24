@echo off
title NBA Bounce Texture Mod Manager - Setup
color 0A
echo.
echo  ==========================================
echo   NBA Bounce Texture Mod Manager - Setup
echo  ==========================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python is not installed or not in PATH.
    echo.
    echo  Please install Python 3.9+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo  [OK] Python found.
echo.
echo  Installing/updating required packages...
echo.

python -m pip install --upgrade pip --quiet
python -m pip install UnityPy Pillow --quiet

if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to install packages.
    echo  Try right-clicking SETUP_AND_RUN.bat and choosing "Run as Administrator".
    pause
    exit /b 1
)

echo.
echo  [OK] All packages installed successfully.
echo.
echo  Launching NBA Bounce Texture Mod Manager...
echo.

python "%~dp0app.py"

pause
