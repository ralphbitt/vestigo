@echo off
rem Abre o Vestigo. Arraste o PRIMEIRO arquivo da imagem (.E01, .001, .dd) sobre este atalho,
rem ou execute:  vestigo.bat "D:\caso\imagem.E01" --saida "D:\caso\recuperados"
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Ambiente nao encontrado. Rode primeiro: instalar.bat
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m vestigo %*
