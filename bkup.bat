@echo off
setlocal
cd /d "%~dp0"

REM Forward all parameters to main_backup.py
python main_backup.py %*
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% neq 0 (
    echo [ERROR] Backup failed with code %EXIT_CODE%.
)

exit /b %EXIT_CODE%