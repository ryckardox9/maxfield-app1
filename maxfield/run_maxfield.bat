@echo off
REM ==============================
REM Maxfield - execução organizada com timestamp
REM ==============================

cd /d "C:\Users\paulo\OneDrive\Documentos\maxfield"
call env\Scripts\activate.bat

if not exist output (
    mkdir output
)

REM Pergunta o nome do arquivo
set /p FILENAME=Digite o nome do arquivo de portais (ex: meu_plano.txt): 

REM Tira a extensão (.txt) para usar no nome da pasta
for %%i in ("%FILENAME%") do set PLANNAME=%%~ni

REM Pega data e hora para criar pasta única
for /f "tokens=1-4 delims=/ " %%a in ("%date%") do set dt=%%a-%%b-%%c
for /f "tokens=1-2 delims=: " %%a in ("%time%") do set tm=%%a%%b
set PLANFOLDER=output\%PLANNAME%_%dt%_%tm%

REM Cria a pasta específica para este plano
if not exist "%PLANFOLDER%" (
    mkdir "%PLANFOLDER%"
)

REM Número de agentes
set NUM_AGENTS=3
set /p NUM_AGENTS=Digite o número de agentes (padrão: 3): 
if "%NUM_AGENTS%"=="" set NUM_AGENTS=3

REM Número de CPUs
set NUM_CPUS=0
set /p NUM_CPUS=Digite o número de CPUs a usar (padrão: 0): 
if "%NUM_CPUS%"=="" set NUM_CPUS=0

REM Executa o Maxfield salvando na pasta específica
python .\bin\maxfield-plan %FILENAME% --num_agents %NUM_AGENTS% --num_cpus %NUM_CPUS% --output_csv -o "%PLANFOLDER%" -v

pause