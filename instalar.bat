@echo off
rem Cria o ambiente Python do Vestigo (uma vez so). Exige Python 3.13 e FFmpeg no PATH.
cd /d "%~dp0"
python -m venv .venv || goto erro
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto erro
rem atalho com o icone do programa, na propria pasta (arraste a imagem sobre ele)
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%~dp0Vestigo.lnk'); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='-m vestigo'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0vestigo\recursos\vestigo.ico'; $s.Save()"
echo.
echo Pronto. Use o atalho Vestigo (ou vestigo.bat)
pause
exit /b 0
:erro
echo Falhou. Confira se o Python 3.13 esta instalado.
pause
exit /b 1
