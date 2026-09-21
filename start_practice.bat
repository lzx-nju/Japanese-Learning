@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动日语练习程序...

rem 找解释器：优先用绝对路径（避开 C:\Windows\System32\python 这个
rem Microsoft Store 应用执行别名占位符，它只会弹「选择应用」窗口），
rem 找不到再依次退回 py 启动器与 PATH 里的 python——换机器时不必改这个文件。
set "PY="
if defined PY goto run
py -3 --version >nul 2>nul && set "PY=py -3"
if defined PY goto run
python --version >nul 2>nul && set "PY=python"
if defined PY goto run
echo [错误] 没找到可用的 Python 3。
echo   装好 Python 3 后重试；或把本机 conda 的 python.exe 路径填到上面的 if exist 行。
echo   排查：分别执行 where python 与 where py，看它们指向哪里。
pause
exit /b 1

:run
echo 使用解释器：%PY%
start "" http://127.0.0.1:5000
%PY% practice\app.py
pause
