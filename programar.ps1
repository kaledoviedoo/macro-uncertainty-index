<#
    programar.ps1 - Registra la secuencia diaria en el Programador de tareas.

    LA HORA. Se ejecuta a las 19:30 de lunes a viernes, hora de Bogota. El
    cierre de Wall Street son las 16:00 de Bogota, y los datos de Yahoo
    tardan un rato en asentarse; a las 19:30 el cierre del dia ya es firme.
    Antes de esa hora se estaria prediciendo con precios a medio formar.

    -WakeToRun despierta el equipo si esta suspendido. Sin eso, un portatil
    cerrado se salta la noche entera y el historial queda con huecos, que
    es justo lo que este proyecto lleva media vida evitando.

    Uso:
        powershell -ExecutionPolicy Bypass -File .\programar.ps1
        powershell -ExecutionPolicy Bypass -File .\programar.ps1 -Quitar
#>

param(
    [switch]$Quitar,
    [string]$Hora = "19:30"
)

$nombre = "MotorCausal-Diario"
$script = Join-Path $PSScriptRoot "diario.ps1"

if ($Quitar) {
    try {
        Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
        Write-Host "Tarea '$nombre' eliminada." -ForegroundColor Green
    } catch {
        Write-Host "No existe ninguna tarea llamada '$nombre'." -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "No encuentro diario.ps1 junto a este script." -ForegroundColor Red
    exit 1
}

$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"") `
    -WorkingDirectory $PSScriptRoot

$disparador = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Hora

$ajustes = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

try {
    Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparador `
        -Settings $ajustes -Description "Motor de inferencia causal: ingesta, extraccion y prediccion diaria" `
        -Force | Out-Null

    Write-Host ""
    Write-Host "  Tarea registrada." -ForegroundColor Green
    Write-Host "  Nombre:   $nombre"
    Write-Host "  Cuando:   lunes a viernes a las $Hora"
    Write-Host "  Ejecuta:  $script"
    Write-Host ""
    Write-Host "  -StartWhenAvailable la lanza en cuanto puedas si el equipo"
    Write-Host "  estaba apagado a esa hora: se pierde puntualidad, no el dia."
    Write-Host ""
    Write-Host "  Para probarla ahora mismo, sin esperar:"
    Write-Host "     Start-ScheduledTask -TaskName $nombre" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Para ver si corrio anoche:"
    Write-Host "     Get-ScheduledTaskInfo -TaskName $nombre" -ForegroundColor Yellow
    Write-Host "     Get-Content .\logs\diario-*.log -Tail 20" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Para quitarla:"
    Write-Host "     .\programar.ps1 -Quitar" -ForegroundColor Yellow
    Write-Host ""
} catch {
    Write-Host ""
    Write-Host "  No se pudo registrar: $_" -ForegroundColor Red
    Write-Host "  Suele ser falta de permisos. Abre PowerShell como"
    Write-Host "  administrador y vuelve a ejecutarlo."
    exit 1
}
