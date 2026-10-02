@echo off
rem Cria o ambiente Python do Vestigo (uma vez so). Exige Python 3.13 e FFmpeg no PATH.
cd /d "%~dp0"
python -m venv .venv || goto erro
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto erro
echo.
echo Pronto. Use vestigo.bat
pause
exit /b 0
:erro
echo Falhou. Confira se o Python 3.13 esta instalado.
pause
exit /b 1
