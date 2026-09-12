@echo off
REM Avvia TubeAssistant in background (finestra ridotta a icona).
REM Richiede la CLI installata: installa.bat oppure `uv tool install -e .`
where tube-assistant >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [!] tube-assistant non trovato nel PATH. Esegui prima installa.bat
    pause
    exit /b 1
)
start "TubeAssistant" /min tube-assistant start
echo Agent avviato in background. Controllalo da Telegram.
