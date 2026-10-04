@echo off
chcp 65001 >nul
cd /d "%~dp0src"
echo.
echo  ============================================================
echo   校园科创导航智能体 本地启动
echo   访问地址: http://127.0.0.1:8011
echo   (保持此窗口打开；关闭窗口即停止服务)
echo  ============================================================
echo.
"%~dp0venv\Scripts\python.exe" -m uvicorn api:app --host 127.0.0.1 --port 8011
echo.
echo 服务已停止。按任意键退出...
pause >nul
