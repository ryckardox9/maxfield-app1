@echo off
REM ==============================
REM Maxfield - execução automática e tradução inteligente
REM ==============================

REM Entrar na pasta principal do projeto
cd /d "C:\Users\paulo\OneDrive\Documentos\maxfield"

REM Ativar a virtualenv
call env\Scripts\activate.bat

REM Criar pasta de saída (se não existir)
if not exist output (
    mkdir output
)

REM Perguntar o arquivo de entrada
set /p FILENAME=Digite o nome do arquivo de portais (ex: meu_plano.txt): 

REM Perguntar número de agentes
set /p NUM_AGENTS=Digite o número de agentes (ex: 3): 

REM Perguntar número de CPUs
set /p NUM_CPUS=Digite o número de CPUs a usar (0 para máximo): 

REM Rodar o Maxfield e salvar resultados na pasta 'output'
python .\bin\maxfield-plan %FILENAME% --num_agents %NUM_AGENTS% --num_cpus %NUM_CPUS% --output_csv -o output -v

REM Verifica se o script de tradução existe
if exist traduzir_maxfield.py (
    echo.
    echo Traduzindo arquivos...
    python traduzir_maxfield.py
) else (
    echo.
    echo Script traduzir_maxfield.py nao encontrado! Nao foi possivel traduzir.
)

REM Mensagem final
echo.
echo ==============================
echo Maxfield finalizado! Arquivos traduzidos em output\pt
echo ==============================
pause