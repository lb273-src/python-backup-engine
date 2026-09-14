@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Prefer the Python Launcher (py -3), then python3, then python
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
    if %errorlevel% equ 0 (
        py -3 main_backup.py %*
        exit /b %errorlevel%
    )
)

where python3 >nul 2>&1
if %errorlevel% equ 0 (
    python3 -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
    if %errorlevel% equ 0 (
        python3 main_backup.py %*
        exit /b %errorlevel%
    )
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    python -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
    if %errorlevel% equ 0 (
        python main_backup.py %*
        exit /b %errorlevel%
    )
)

echo [ERROR] Python 3.8 or newer is required and could not be found in PATH.
exit /b 1