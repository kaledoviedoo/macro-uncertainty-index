<#
    diario.ps1 - La secuencia nocturna completa.

    El marcador no significa nada con 80 predicciones de un solo dia. Solo
    empieza a discriminar entre metodos cuando hay cientos, y eso solo pasa
    si el motor se ejecuta TODOS los dias sin que nadie se acuerde.

    ORDEN Y CRITICIDAD. Los pasos no valen lo mismo. Si falla la ingesta de
    noticias, las predicciones del dia se emiten igual con lo que haya: es
    peor perder un dia de historial que perder unas noticias. Pero si fallan
    los precios, NO se predice, porque predecir con datos viejos y
    registrarlo como si fuera de hoy contamina el marcador para siempre.

    Cada paso queda en un log con marca de tiempo. Si algo se rompe una
    noche a las 19:30, el log es lo unico que lo va a contar.

    Uso manual:
        .\diario.ps1
        .\diario.ps1 -SoloCriticos     omite noticias y LLM

    Para programarlo:
        .\programar.ps1
#>

param(
    [switch]$SoloCriticos
)

Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = "utf-8"

$py = Join-Path $env:USERPROFILE ".venvs\motor-causal\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No encuentro el entorno virtual en $py" -ForegroundColor Red
    exit 1
}

$carpetaLog = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $carpetaLog)) { New-Item -ItemType Directory -Path $carpetaLog | Out-Null }
$log = Join-Path $carpetaLog ("diario-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Escribir($texto, $color = "Gray") {
    $linea = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $texto
    Write-Host $linea -ForegroundColor $color
    Add-Content -Path $log -Value $linea -Encoding utf8
}

# critico = si falla, no se sigue a los pasos que dependen de datos frescos
$pasos = @(
    @{ n = "precios";    s = "scripts\ingestar_precios.py";        a = @();              critico = $true  }
    @{ n = "banrep";     s = "scripts\ingestar_banrep.py";         a = @("--escribir");  critico = $false }
    @{ n = "colombia";   s = "scripts\ingestar_datos_abiertos.py"; a = @("--escribir");  critico = $false }
    @{ n = "noticias";   s = "scripts\ingestar_noticias.py";       a = @();              critico = $false; opcional = $true }
    @{ n = "enriquecer"; s = "scripts\enriquecer.py";              a = @("--limite","25"); critico = $false; opcional = $true }
    @{ n = "extraer";    s = "scripts\extraer.py";                 a = @("--limite","15"); critico = $false; opcional = $true }
    @{ n = "predecir";   s = "scripts\predecir.py";                a = @();              critico = $true  }
    @{ n = "resolver";   s = "scripts\resolver.py";                a = @();              critico = $true  }
)

Escribir "=========================================================" "Cyan"
Escribir "SECUENCIA DIARIA" "Cyan"
Escribir "=========================================================" "Cyan"

$fallosCriticos = 0
$resumen = @()

foreach ($p in $pasos) {
    if ($SoloCriticos -and $p.opcional) {
        Escribir ("  {0,-12} omitido (-SoloCriticos)" -f $p.n) "DarkGray"
        continue
    }
    # Un fallo critico previo invalida los pasos criticos posteriores:
    # predecir con precios de anteayer y fecharlo hoy corrompe el marcador.
    if ($fallosCriticos -gt 0 -and $p.critico) {
        Escribir ("  {0,-12} OMITIDO: un paso critico anterior fallo" -f $p.n) "Red"
        $resumen += "$($p.n): omitido"
        continue
    }

    Escribir ("  {0,-12} ejecutando..." -f $p.n)
    $salida = & $py $p.s @($p.a) 2>&1
    $codigo = $LASTEXITCODE
    Add-Content -Path $log -Value ($salida | Out-String) -Encoding utf8

    if ($codigo -eq 0) {
        Escribir ("  {0,-12} OK" -f $p.n) "Green"
        $resumen += "$($p.n): ok"
    } else {
        $nivel = if ($p.critico) { "CRITICO" } else { "aviso" }
        Escribir ("  {0,-12} FALLO ({1}, codigo {2})" -f $p.n, $nivel, $codigo) "Red"
        $resumen += "$($p.n): FALLO"
        if ($p.critico) { $fallosCriticos++ }
    }
}

Escribir "=========================================================" "Cyan"
Escribir ("Resumen: " + ($resumen -join "  |  "))
Escribir ("Log completo: " + $log)

if ($fallosCriticos -gt 0) {
    Escribir "Hubo fallos criticos. Revisa el log antes de fiarte del marcador." "Red"
    exit 1
}
Escribir "Secuencia completa." "Green"
exit 0
