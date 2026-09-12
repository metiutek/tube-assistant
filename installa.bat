@echo off
echo ============================================
echo  TubeAssistant - Installation
echo ============================================
echo.

REM Check if uv is available, fall back to pip
where uv >nul 2>&1
if %ERRORLEVEL% == 0 (
    echo Using uv ^(fast installer^)...
    uv tool install -e .
) else (
    echo uv not found - installing it first...
    pip install uv -q
    uv tool install -e .
)

echo.
echo ============================================
echo  DONE. Now run the setup wizard:
echo.
echo    tube-assistant onboard
echo.
echo  The wizard will guide you through:
echo   1. Agent language
echo   2. AI service ^(OpenRouter / OpenAI / Anthropic / Gemini / Ollama / ...^)
echo   3. Thumbnail image provider
echo   4. Pexels API key ^(free stock clips^)
echo   5. Clip source ^(Pexels stock or AI video^)
echo   6. Telegram bot ^(your control panel^)
echo   7. Channel setup via Telegram + Google credentials
echo.
echo  After setup:
echo    tube-assistant start       ^<- run daemon
echo    tube-assistant             ^<- interactive menu
echo    avvia_agente.bat           ^<- run in background
echo ============================================
pause
