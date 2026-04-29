@echo off
title DirectMailer — Build EXE
echo ============================================
echo  DirectMailer EXE Builder
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    pause & exit /b 1
)

:: Install deps
echo [1/3] Installing dependencies...
pip install pyinstaller dnspython PySocks --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause & exit /b 1
)

:: Build
echo [2/3] Building EXE with PyInstaller...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "DirectMailer" ^
    --icon "sb.ico" ^
    --hidden-import dns.resolver ^
    --hidden-import dns.rdatatype ^
    --hidden-import dns.rdataclass ^
    --hidden-import socks ^
    --hidden-import email.mime.multipart ^
    --hidden-import email.mime.text ^
    --hidden-import email.mime.base ^
    direct_mailer.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause & exit /b 1
)

echo.
echo [3/3] Done!
echo.
echo  EXE location:  dist\DirectMailer.exe
echo.
echo  You can copy dist\DirectMailer.exe anywhere — no Python needed.
echo ============================================
pause
