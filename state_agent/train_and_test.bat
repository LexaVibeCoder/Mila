@echo off
REM Train the State Agent MLP and test it interactively.
REM
REM Usage:
REM   state_agent\train_and_test.bat              train if state_agent.pt is missing/unloadable, then test
REM   state_agent\train_and_test.bat --retrain    force retraining on dataset.json
REM   state_agent\train_and_test.bat --no-test    train (or skip) and exit without testing
REM   state_agent\train_and_test.bat -h           show this help
REM
REM The script uses state_agent\.venv, creating it with Python 3.11 (via uv if
REM needed) and installing state_agent\requirements.txt only when the imports
REM fail. An existing working venv and an existing model are left untouched.
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%SCRIPT_DIR%requirements.txt"
set "MODEL_PATH=%SCRIPT_DIR%state_agent.pt"

set "RETRAIN=0"
set "RUN_TEST=1"

REM ---------------------------------------------------------------- arguments
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--retrain" (
    set "RETRAIN=1"
    shift
    goto :parse_args
)
if /i "%~1"=="--no-test" (
    set "RUN_TEST=0"
    shift
    goto :parse_args
)
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--help" goto :usage
echo ERROR: unknown argument: %~1 ^(see --help^) 1>&2
exit /b 1

:usage
echo Usage: state_agent\train_and_test.bat [--retrain] [--no-test]
exit /b 0

:args_done
echo === Mila State Agent: train ^& test ===
echo Project: %PROJECT_ROOT%

REM ------------------------------------------------ 1. venv + dependencies
echo.
echo --^> Python environment: %VENV_DIR%
if exist "%VENV_DIR%\" if not exist "%VENV_PY%" (
    echo ERROR: %VENV_DIR% exists but %VENV_PY% is missing. 1>&2
    echo        Remove the broken venv and re-run. 1>&2
    exit /b 1
)

if exist "%VENV_PY%" (
    echo --^> Using existing venv, not recreating it
    goto :venv_ready
)

REM Try the py launcher with 3.11.
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYBIN=py -3.11"
    echo --^> Found system Python 3.11 via py launcher
    goto :make_venv
)

REM Try plain python and check for 3.11 in its version string.
python --version 2>&1 | findstr /C:"3.11" >nul 2>&1
if not errorlevel 1 (
    set "PYBIN=python"
    echo --^> Found system Python 3.11 via python
    goto :make_venv
)

echo --^> System Python 3.11 not available, using uv
goto :via_uv

:make_venv
%PYBIN% -m venv "%VENV_DIR%"
if not errorlevel 1 (
    echo --^> venv created with system Python 3.11
    goto :venv_ready
)
echo --^> python -m venv failed, falling back to uv
if exist "%VENV_DIR%\" rmdir /s /q "%VENV_DIR%" 2>nul

:via_uv
call :ensure_uv
if errorlevel 1 exit /b 1
uv venv --python 3.11 "%VENV_DIR%"
if errorlevel 1 exit /b 1
echo --^> venv created with uv ^(Python 3.11^)

:venv_ready
if not exist "%VENV_PY%" (
    echo ERROR: venv python not found at %VENV_PY% 1>&2
    exit /b 1
)
"%VENV_PY%" --version
if errorlevel 1 (
    echo ERROR: venv python is not runnable: %VENV_PY% 1>&2
    exit /b 1
)

REM uv venvs ship without pip by default -> install it if missing.
"%VENV_PY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
echo --^> pip missing in venv, installing pip...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
if not errorlevel 1 (
    "%VENV_PY%" -m pip --version >nul 2>&1
    if not errorlevel 1 goto :deps_check
)
call :ensure_uv
if errorlevel 1 exit /b 1
uv pip install --python "%VENV_PY%" pip
if errorlevel 1 (
    echo ERROR: cannot install pip into the venv. 1>&2
    exit /b 1
)

:deps_check
if not exist "%REQ_FILE%" (
    echo ERROR: requirements.txt not found at %REQ_FILE% 1>&2
    exit /b 1
)
"%VENV_PY%" -c "import torch, sklearn, numpy, tqdm" >nul 2>&1
if not errorlevel 1 (
    echo --^> Dependencies already available, skipping install
    goto :train_step
)
echo --^> Installing dependencies from %REQ_FILE% ^(may take a while^)...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%VENV_PY%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo ERROR: dependency installation failed. 1>&2
    exit /b 1
)
echo --^> Dependencies installed

REM ------------------------------------ 2. training (skipped if model exists)
:train_step
echo.
cd /d "%SCRIPT_DIR%"
if errorlevel 1 (
    echo ERROR: cannot cd to %SCRIPT_DIR% 1>&2
    exit /b 1
)

set "MODEL_OK=0"
if exist "%MODEL_PATH%" (
    "%VENV_PY%" test_interactive.py --check >nul 2>&1
    if not errorlevel 1 set "MODEL_OK=1"
)
if "%RETRAIN%"=="1" goto :do_train
if "%MODEL_OK%"=="1" goto :model_exists

:do_train
if exist "%MODEL_PATH%" if "%RETRAIN%"=="0" echo --^> Model file exists but failed to load; retraining: %MODEL_PATH%
echo --^> Training on dataset.json...
"%VENV_PY%" train_state_agent.py
if errorlevel 1 (
    echo ERROR: training failed. 1>&2
    exit /b 1
)
if not exist "%MODEL_PATH%" (
    echo ERROR: training finished but %MODEL_PATH% was not created. 1>&2
    exit /b 1
)
echo --^> Model saved: %MODEL_PATH%
goto :test_step

:model_exists
echo --^> Model exists and loads: %MODEL_PATH%
echo --^> Skipping training ^(use --retrain to force retraining^)

REM ------------------------------------------------------ 3. interactive test
:test_step
if "%RUN_TEST%"=="0" (
    echo --^> --no-test given, done
    exit /b 0
)
echo.
set "ANS="
set /p "ANS=Run the interactive test? [Y/n]: "
if /i "%ANS%"=="n" (
    echo --^> Testing skipped
    exit /b 0
)
if /i "%ANS%"=="no" (
    echo --^> Testing skipped
    exit /b 0
)

"%VENV_PY%" test_interactive.py
exit /b %errorlevel%

REM ------------------------------------------------------------- subroutines
:ensure_uv
where uv >nul 2>&1
if not errorlevel 1 exit /b 0
echo --^> uv not found, installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo ERROR: uv installation failed. 1>&2
    exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found after install. Check %USERPROFILE%\.local\bin 1>&2
    exit /b 1
)
for /f "delims=" %%V in ('uv --version') do echo --^> uv installed: %%V
exit /b 0
