@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"

where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 goto use_py_launcher
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 goto use_python
)

where python3.exe >nul 2>nul
if not errorlevel 1 (
    python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 goto use_python3
)

echo [ERROR] Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/ and try again.
set "INSTALL_EXIT=1"
goto finish

:use_py_launcher
py -3 "%~dp0install.py" %*
set "INSTALL_EXIT=%ERRORLEVEL%"
goto finish

:use_python
python "%~dp0install.py" %*
set "INSTALL_EXIT=%ERRORLEVEL%"
goto finish

:use_python3
python3 "%~dp0install.py" %*
set "INSTALL_EXIT=%ERRORLEVEL%"

:finish
echo.
if not defined VIBEAI_NO_PAUSE pause
exit /b %INSTALL_EXIT%
