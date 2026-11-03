# Schedules, removes or runs the weekly Radar check (Mondays at 10:00), which
# shows a notification when projects have work that isn't backed up.
#   .\weekly.ps1 install     .\weekly.ps1 uninstall     .\weekly.ps1 run
param(
  [Parameter(Mandatory = $true)][ValidateSet('install', 'uninstall', 'run')][string]$Action,
  [string]$Exe = (Join-Path $env:LOCALAPPDATA 'Programs\Radar\Radar.exe')
)
$ErrorActionPreference = 'Stop'
$task = 'Radar weekly check'

switch ($Action) {
  'install' {
    if (-not (Test-Path $Exe)) { throw "Install Radar first: .\build.ps1 ($Exe not found)" }
    schtasks /Create /F /SC WEEKLY /D MON /ST 10:00 /TN $task /TR "`"$Exe`" --notify" | Out-Null
    Write-Host "Weekly check scheduled (Mondays 10:00)."
  }
  'uninstall' {
    schtasks /Delete /F /TN $task | Out-Null
    Write-Host 'Weekly check removed.'
  }
  'run' {
    schtasks /Run /TN $task | Out-Null
    Write-Host 'Started.'
  }
}
