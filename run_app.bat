@echo off
title Mark Minervini Trend Template & VCP Screener
cd /d "%~dp0"

echo =====================================================================
echo   Mark Minervini Trend Template & VCP Stock Screener
echo =====================================================================
echo.
echo [알림] Streamlit 웹 서버를 시작합니다...
echo [알림] 서버 구동이 완료되면 브라우저가 자동으로 열립니다.
echo.
echo 앱 실행을 종료하려면 이 창을 닫거나 콘솔에서 [Ctrl + C]를 누르세요.
echo ---------------------------------------------------------------------
echo.

python -m streamlit run app.py --server.headless false

if errorlevel 1 (
    echo.
    echo [오류] 앱 실행 중 문제가 발생했습니다.
    echo 파이썬 환경 및 패키지 설치(pip install -r requirements.txt)를 확인하세요.
    echo.
    pause
)
