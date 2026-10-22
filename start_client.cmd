@echo off
setlocal
set "CODOL_PYTHON=C:\Python314\python.exe"
if not exist "%CODOL_PYTHON%" (
    echo Python not found: %CODOL_PYTHON%
    pause
    exit /b 1
)
echo CODOL 1649370 Local Full-Unlock Test
echo Resource validation may take a while. UAC is for the temporary offline guard.
echo Local account: data\accounts. Server: http://127.0.0.1:8080
"%CODOL_PYTHON%" -B -X utf8 "%~dp0tools\launch_client.py" --resource-library "%~dp0resources\v1649370\offline-image-library" --loose-library "%~dp0resources\v1649370\offline-loose-library" --local-profile --server-url http://127.0.0.1:8080 %*
set "CODOL_EXIT=%ERRORLEVEL%"
if not "%CODOL_EXIT%"=="0" (
    echo Launch or runtime failed. Exit code: %CODOL_EXIT%. Please keep the log.
    pause
)
exit /b %CODOL_EXIT%
