@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Digital Human Local Services (TTS + ASR + Channel Stub)
cd /d F:\digital-human\services

set "PY=F:\digital-human\envs\voxcpm\Scripts\python.exe"

echo Restarting local services. First launch may take a few minutes...
echo Keep this window open after startup, AIRI needs these services.
echo.

rem ============================================================
rem  Stop phase: kill by command-line pattern, not only by port.
rem  Reason: the conda env python.exe is now a forwarder shim;
rem  the real service runs under the daimon runtime python.
rem  Killing only the port owner leaves shim processes behind,
rem  and repeated runs would stack up duplicate processes.
rem ============================================================
set /a ROUND=0
:kill_loop
set /a ROUND+=1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'voxcpm_server\.py|whisper_server\.py|sensevoice_server\.py|channel_stub_server\.py|claude_bridge_server\.py' } | ForEach-Object { Write-Host ('  kill PID=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
if %ROUND% GEQ 3 goto kill_done
ping -n 3 127.0.0.1 >nul
goto kill_loop
:kill_done

rem Verify ports are free; warn if something still holds them.
set /a BUSY=0
netstat -ano | findstr ":8930" | findstr "LISTENING" >nul && set /a BUSY+=1
netstat -ano | findstr ":8931" | findstr "LISTENING" >nul && set /a BUSY+=1
netstat -ano | findstr ":6121" | findstr "LISTENING" >nul && set /a BUSY+=1
netstat -ano | findstr ":8932" | findstr "LISTENING" >nul && set /a BUSY+=1
if %BUSY% GTR 0 (
    echo [WARNING] %BUSY% ports still busy. Some processes may not be killable.
    echo           Please end python.exe in Task Manager, then rerun this script.
    pause
    exit /b 1
)
echo Old processes cleaned, ports released.
echo.

rem ============================================================
rem  Start phase: skip start if a process is already running.
rem ============================================================
call :start_one "VoxCPM TTS" voxcpm_server.py 8930
call :start_one "SenseVoiceSmall ASR" sensevoice_server.py 8931
call :start_one "AIRI Channel Stub" channel_stub_server.py 6121
call :start_one "Claude Bridge" "F:\digital-human\claude-bridge\claude_bridge_server.py" 8932

rem ============================================================
rem  Web frontend: AIRI page on http://localhost:5173
rem  Skip if the port is already listening, never duplicate.
rem ============================================================
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo [skip] Web frontend already running on 5173.
) else (
    start "AIRI Web 5173" "F:\digital-human\services\start-web5173.bat"
    echo [start] AIRI Web 5173 (vite preview, ready in ~15s)
)

echo.
echo Done. ASR and channel stub are ready immediately;
echo TTS model loading takes about 1-2 minutes.
ping -n 6 127.0.0.1 >nul
exit /b 0

rem ---------- subroutine: start one service, never duplicate ----------
:start_one
set "WINTITLE=%~1"
set "SCRIPT=%~2"
set "SPORT=%~3"
powershell -NoProfile -Command "if ((Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '%SCRIPT%' } | Measure-Object).Count -gt 0) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo [skip] %WINTITLE% already running, not starting a duplicate.
) else (
    if not exist logs mkdir logs
    for %%F in ("%SCRIPT%") do set LOGNAME=%%~nF
    rem Tee-Object on PS 5.1 writes UTF-16LE, so hand-roll the tee:
    rem echo each line to console AND append as UTF-8 no-BOM to the log file.
    start "%WINTITLE%" powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%PY%' -u %SCRIPT% *>&1 | ForEach-Object { $l = $_.ToString(); $l; [System.IO.File]::AppendAllText('logs\!LOGNAME!.log', $l + \"`n\", (New-Object System.Text.UTF8Encoding $false)) }"
    echo [start] %WINTITLE% %SCRIPT% port %SPORT%
)
exit /b 0