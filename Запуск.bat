@echo off
cd /d "%~dp0"

set "PY="
set "PYW="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3" && set "PYW=pyw -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python" && set "PYW=pythonw"
if not defined PY goto nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 goto oldpython

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto notk

%PY% -c "import telethon, python_socks, qrcode" >nul 2>nul
if not errorlevel 1 goto run

echo First run: installing required packages, please wait...
%PY% -m pip install --disable-pip-version-check telethon qrcode "python-socks[asyncio]"
if errorlevel 1 goto pipfail

:run
%PY% -c "import cryptg" >nul 2>nul
if errorlevel 1 %PY% -m pip install --disable-pip-version-check cryptg >nul 2>nul
start "" %PYW% "%~dp0tg_export_gui.py"
exit /b 0

:nopython
echo.
echo Python is not installed (or not added to PATH).
echo 1. Download Python 3.11+ from python.org (the page will open now).
echo 2. During setup TICK the checkbox "Add python.exe to PATH".
echo 3. Run this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:oldpython
echo.
echo Your Python is too old. Install Python 3.11+ from python.org and run this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:notk
echo.
echo Python is installed without Tkinter (needed for the window).
echo Reinstall Python from python.org and keep "tcl/tk and IDLE" enabled.
pause
exit /b 1

:pipfail
echo.
echo Could not install packages. Check your internet / VPN connection and run this file again.
pause
exit /b 1
