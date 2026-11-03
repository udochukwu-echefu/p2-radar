# Schedules, removes or runs the weekly P2 Radar check (Mondays at 10:00), which
# shows a notification when projects have work that isn't backed up.
#   .\weekly.ps1 install     .\weekly.ps1 uninstall     .\weekly.ps1 run
param(
  [Parameter(Mandatory = $true)][ValidateSet('install', 'uninstall', 'run')][string]$Action,
  [string]$Exe = (Join-Path $env:LOCALAPPDATA 'Programs\P2 Radar\P2 Radar.exe')
)
$ErrorActionPreference = 'Stop'
$task = 'P2 Radar weekly check'
$oldTask = 'Radar weekly check'   # its name before the app became P2 Radar

switch ($Action) {
  'install' {
    if (-not (Test-Path $Exe)) { throw "Install P2 Radar first: .\build.ps1 ($Exe not found)" }
    schtasks /Delete /F /TN $oldTask 2>$null | Out-Null
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
