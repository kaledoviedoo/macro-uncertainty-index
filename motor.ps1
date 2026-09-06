<#
    motor.ps1 - Lanzador del proyecto.

    Evita tener que recordar la ruta del entorno virtual y volver a definir
    $py cada vez que abres una ventana nueva de PowerShell. Las variables no
    sobreviven al cierre de la terminal; un script si.

    NOTA DE CODIFICACION: este archivo se guarda con BOM UTF-8 y su codigo
    es ASCII puro, sin tildes ni enes. PowerShell 5.1 lee los .ps1 como ANSI
    cuando no encuentra BOM, y ahi una "O" con tilde se convierte en dos
    bytes que rompen el parser dentro de una tabla hash.

    Uso:
        .\motor.ps1                    lista los comandos
        .\motor.ps1 estado             panel de control
        .\motor.ps1 app                la Terminal Optica en :8050
        .\motor.ps1 noticias
        .\motor.ps1 extraer --limite 5 --seco

    Si Windows bloquea el script:
        powershell -ExecutionPolicy Bypass -File .\motor.ps1 estado
#>

param(
    [string]$Comando = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Que la consola muestre bien los acentos que imprime Python.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = "utf-8"

$py = Join-Path $env:USERPROFILE ".venvs\motor-causal\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "No encuentro el entorno virtual en:" -ForegroundColor Red
    Write-Host "  $py"
    Write-Host ""
    Write-Host "Crealo con:  powershell -ExecutionPolicy Bypass -File .\setup.ps1"
    exit 1
}

$comandos = [ordered]@{
    "estado"     = @{ s = "scripts\estado.py";                  a = @();                 d = "Panel de control: cobertura, huecos, regimen, fuentes" }
    "app"        = @{ s = "app.py";                             a = @();                 d = "La Terminal Optica en http://127.0.0.1:8050" }
    "verificar"  = @{ s = "scripts\verificar.py";               a = @();                 d = "Comprueba tickers y feeds contra el mundo real" }
    "precios"    = @{ s = "scripts\ingestar_precios.py";        a = @();                 d = "Ingesta diaria de precios y regimen" }
    "historico"  = @{ s = "scripts\ingestar_precios.py";        a = @("--historico");    d = "Carga inicial de 10 anos" }
    "regimen"    = @{ s = "scripts\ingestar_precios.py";        a = @("--solo-regimen"); d = "Recalcula solo la clasificacion de regimen" }
    "banrep"     = @{ s = "scripts\ingestar_banrep.py";         a = @("--escribir");     d = "Series del Banco de la Republica" }
    "colombia"   = @{ s = "scripts\ingestar_datos_abiertos.py"; a = @("--escribir");     d = "TRM desde datos.gov.co" }
    "noticias"   = @{ s = "scripts\ingestar_noticias.py";       a = @();                 d = "Ingesta de noticias por RSS" }
    "enriquecer" = @{ s = "scripts\enriquecer.py";              a = @();                 d = "Descarga el texto completo de las fuentes oficiales" }
    "extraer"    = @{ s = "scripts\extraer.py";                 a = @();                 d = "Lote de extraccion con LLM (necesita API key)" }
    "calendario" = @{ s = "scripts\calendario.py";              a = @("--proximos");     d = "Proximos eventos de fecha conocida" }
    "calibrar"   = @{ s = "scripts\calendario.py";              a = @("--calibrar");     d = "Mide el impacto historico de cada evento" }
    "senales"    = @{ s = "scripts\evaluar.py";                 a = @("--todos");        d = "Marcador de senales de caida, en muestra" }
    "caidas"     = @{ s = "scripts\modelo_caidas.py";           a = @();                 d = "Modelo de caidas validado fuera de muestra" }
    "predecir"   = @{ s = "scripts\predecir.py";                a = @();                 d = "FASE 6: emite las predicciones fechadas de hoy" }
    "resolver"   = @{ s = "scripts\resolver.py";                a = @();                 d = "FASE 6: cobra las vencidas y muestra el marcador" }
    "marcador"   = @{ s = "scripts\resolver.py";                a = @("--marcador");     d = "Solo el marcador, sin resolver nada" }
    "sinapsis"   = @{ s = "scripts\sinapsis.py";                a = @();                 d = "El grafo causal vigente, con cita y aritmetica" }
}

if (-not $Comando -or -not $comandos.Contains($Comando)) {
    if ($Comando) {
        Write-Host ""
        Write-Host "  Comando desconocido: '$Comando'" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  MOTOR DE INFERENCIA CAUSAL" -ForegroundColor Cyan
    Write-Host "  ------------------------------------------------------------------"
    foreach ($k in $comandos.Keys) {
        Write-Host "  .\motor.ps1 " -NoNewline
        Write-Host $k.PadRight(12) -ForegroundColor Green -NoNewline
        Write-Host $comandos[$k].d -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "  Los argumentos extra se pasan tal cual al script:" -ForegroundColor DarkGray
    Write-Host "    .\motor.ps1 extraer --limite 5 --seco" -ForegroundColor DarkGray
    Write-Host "    .\motor.ps1 caidas --caida 3 --dias 5" -ForegroundColor DarkGray
    Write-Host ""
    exit 0
}

$c = $comandos[$Comando]

# El @( ) de fuera no es decorativo. PowerShell DESENVUELVE un array de un
# solo elemento cuando sale de un bloque if, asi que con un unico argumento
# esto quedaba como la cadena "--modelos" en vez de como un array de uno.
# Y el operador @ aplicado a una cadena reparte sus CARACTERES: Python
# recibia "- - m o d e l o s" como nueve argumentos distintos.
# El [string[]] refuerza el tipo por si acaso.
[string[]]$args_finales = @(
    if ($Extra -and $Extra.Count -gt 0) { $Extra } else { $c.a }
)

Write-Host ""
Write-Host "  > $($c.s) $($args_finales -join ' ')" -ForegroundColor DarkGray
Write-Host ""

if ($args_finales.Count -gt 0) {
    & $py $c.s @args_finales
} else {
    & $py $c.s
}
exit $LASTEXITCODE
