<#
    setup.ps1 — Arranque de la Fase 1 en Windows.

    Detecta Python, crea un entorno virtual FUERA de OneDrive, instala las
    dependencias y ejecuta la verificación.

    Cómo ejecutarlo (PowerShell, dentro de la carpeta finance):

        powershell -ExecutionPolicy Bypass -File .\setup.ps1

    El -ExecutionPolicy Bypass hace falta porque Windows bloquea por defecto
    los scripts .ps1 descargados. Solo afecta a esta ejecución.
#>

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Write-Titulo($texto) {
    Write-Host ""
    Write-Host ("=" * 66) -ForegroundColor DarkGray
    Write-Host $texto -ForegroundColor Cyan
    Write-Host ("=" * 66) -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------
# 1. Encontrar un Python que funcione de verdad
#
# Ojo con la trampa de Windows: existe un "python.exe" falso en WindowsApps
# que no es Python, solo abre la Microsoft Store. Por eso no basta con
# comprobar que el comando existe: hay que ejecutarlo y ver si responde.
# ---------------------------------------------------------------------
Write-Titulo "1/4  Buscando Python"

$candidatos = @(
    @{ Exe = 'py';      Args = @('-3') },
    @{ Exe = 'python';  Args = @()     },
    @{ Exe = 'python3'; Args = @()     }
)

$python = $null
foreach ($c in $candidatos) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $version = & $c.Exe @($c.Args) -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match '^\d+\.\d+$') {
            $python = $c
            $pyVersion = $version
            $ruta = (& $c.Exe @($c.Args) -c "import sys; print(sys.executable)")
            Write-Host "  Encontrado: Python $version" -ForegroundColor Green
            Write-Host "  Ruta: $ruta" -ForegroundColor DarkGray
            break
        }
    } catch { }
}

if (-not $python) {
    Write-Host ""
    Write-Host "  No hay Python instalado en este equipo." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Instálalo con UNA de estas dos opciones:"
    Write-Host ""
    Write-Host "  A) Desde PowerShell (la más rápida):" -ForegroundColor Yellow
    Write-Host "       winget install Python.Python.3.12"
    Write-Host ""
    Write-Host "  B) Desde la web:" -ForegroundColor Yellow
    Write-Host "       https://www.python.org/downloads/"
    Write-Host "       IMPORTANTE: marca la casilla 'Add python.exe to PATH'"
    Write-Host "       en la primera pantalla del instalador."
    Write-Host ""
    Write-Host "  Después CIERRA esta ventana de PowerShell, abre una nueva"
    Write-Host "  (el PATH no se refresca en ventanas ya abiertas) y vuelve"
    Write-Host "  a ejecutar este script."
    Write-Host ""
    exit 1
}

$partes = $pyVersion.Split('.')
if ([int]$partes[0] -lt 3 -or ([int]$partes[0] -eq 3 -and [int]$partes[1] -lt 10)) {
    Write-Host "  Python $pyVersion es demasiado antiguo. Hace falta 3.10 o superior." -ForegroundColor Red
    Write-Host "  Instala una versión nueva:  winget install Python.Python.3.12"
    exit 1
}

# ---------------------------------------------------------------------
# 2. Entorno virtual, fuera de OneDrive
#
# Un venv son miles de archivos pequeños. Dentro de una carpeta sincronizada
# con OneDrive, la sincronización se vuelve lenta y a veces corrompe archivos
# en uso. Por eso vive en tu perfil de usuario, no junto al proyecto.
# ---------------------------------------------------------------------
Write-Titulo "2/4  Preparando el entorno virtual"

$venv = Join-Path $env:USERPROFILE ".venvs\motor-causal"
$venvPy = Join-Path $venv "Scripts\python.exe"

if (Test-Path $venvPy) {
    Write-Host "  Ya existe: $venv" -ForegroundColor DarkGray
} else {
    Write-Host "  Creando en: $venv"
    & $python.Exe @($python.Args) -m venv $venv
    if (-not (Test-Path $venvPy)) {
        Write-Host "  No se pudo crear el entorno virtual." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Listo." -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 3. Dependencias
# ---------------------------------------------------------------------
Write-Titulo "3/4  Instalando dependencias"

& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Falló la instalación. Revisa el error de arriba." -ForegroundColor Red
    exit 1
}
Write-Host "  Dependencias instaladas." -ForegroundColor Green

# ---------------------------------------------------------------------
# 4. Verificación contra el mundo real
# ---------------------------------------------------------------------
Write-Titulo "4/4  Verificando tickers y feeds"

if (-not (Test-Path ".env")) {
    Write-Host "  Falta el archivo .env." -ForegroundColor Red
    Write-Host "  Copia la plantilla y pega tu service_role key:"
    Write-Host "     Copy-Item .env.example .env"
    exit 1
}

& $venvPy "scripts\verificar.py"

Write-Titulo "Hecho"
Write-Host "  Para volver a ejecutar cualquier script del proyecto:"
Write-Host ""
Write-Host "     $venvPy scripts\verificar.py" -ForegroundColor Yellow
Write-Host ""
Write-Host "  O activa el entorno una vez por sesión de terminal:"
Write-Host ""
Write-Host "     & `"$venv\Scripts\Activate.ps1`"" -ForegroundColor Yellow
Write-Host "     python scripts\verificar.py"
Write-Host ""
