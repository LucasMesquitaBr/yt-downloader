@echo off
chcp 65001 > nul
title YouTube Downloader

echo.
echo  ╔══════════════════════════════════════╗
echo  ║     YouTube Downloader - Setup       ║
echo  ╚══════════════════════════════════════╝
echo.

:: Detectar Python
set "PY_CMD=python"
python --version > nul 2>&1
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    ) else (
        echo [ERRO] Python nao encontrado no PATH nem em AppData!
        echo Instale o Python em: https://www.python.org/downloads/
        echo Marque a opcao "Add Python to PATH" durante a instalacao.
        pause
        exit /b 1
    )
)

echo [OK] Python encontrado (%PY_CMD%).

:: Criar venv se não existir
if not exist ".venv" (
    echo [*] Criando ambiente virtual...
    "%PY_CMD%" -m venv .venv
)

echo [*] Instalando dependencias...
.venv\Scripts\pip install -q -r requirements.txt

echo.
echo [OK] Tudo pronto! Abrindo o site...
echo [*] Acesse: http://localhost:5000
echo [*] Pressione Ctrl+C para parar o servidor.
echo.

:: Abrir browser automaticamente
start http://localhost:5000

:: Iniciar o servidor
.venv\Scripts\python app.py

pause
