@echo off
cd /d "%~dp0"

where streamlit >nul 2>nul
if %errorlevel% equ 0 (
    streamlit run app.py --server.port 8503
    goto END
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -m streamlit run app.py --server.port 8503
    goto END
)

echo [ERROR] Streamlit could not be found. Please run: pip install -r requirements.txt

:END
echo.
echo ============================================================
echo  Execution finished. Press any key to close this window.
echo ============================================================
pause >nul
