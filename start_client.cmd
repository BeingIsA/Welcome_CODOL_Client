@echo off
setlocal
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH. Please install Python or add it to PATH.
    pause
    exit /b 1
)
set "CFG=%~dp0config\server.ini"
if not exist "%CFG%" (
    echo Config not found: %CFG%
    pause
    exit /b 1
)
echo CODOL 1649370 Local Full-Unlock Test
echo Resource validation may take a while. UAC is for the temporary offline guard.
echo Local account: data\accounts. Server configuration: %CFG%
python -B -X utf8 "%~dp0tools\launch_client.py" --native-library "%~dp0resources\v1649370\game" --local-profile --server-config "%CFG%" %*
set "CODOL_EXIT=%ERRORLEVEL%"
if not "%CODOL_EXIT%"=="0" (
    echo Launch or runtime failed. Exit code: %CODOL_EXIT%. Please keep the log.
    pause
)
exit /b %CODOL_EXIT%
