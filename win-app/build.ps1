# Builds Radar for Windows and installs it for the current user.
#   .\build.ps1                build, install to %LOCALAPPDATA%\Programs\Radar, add a Start menu shortcut
#   .\build.ps1 -Weekly        also schedule the weekly check (Mondays 10:00)
#   .\build.ps1 -NoInstall     build only (into .\publish)
# Needs the .NET 8 SDK (https://dotnet.microsoft.com/download) and Python 3.8+.
param(
  [switch]$Weekly,
  [switch]$NoInstall,
  [string]$Dest = (Join-Path $env:LOCALAPPDATA 'Programs\Radar')
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$out = Join-Path $here 'publish'

Write-Host '-> build'
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
dotnet publish (Join-Path $here 'Radar.csproj') -c Release -r win-x64 --self-contained true `
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o $out
if ($LASTEXITCODE -ne 0) { throw 'dotnet publish failed' }

Write-Host '-> engine'
$engine = Join-Path $out 'engine'
New-Item -ItemType Directory -Force $engine | Out-Null
Copy-Item (Join-Path $root 'project-radar\*.py') $engine
Copy-Item (Join-Path $root 'project-radar\dashboard.html') $engine

if ($NoInstall) { Write-Host "Built $out"; return }

Write-Host '-> install'
Get-Process Radar -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force $Dest | Out-Null
Copy-Item (Join-Path $out '*') $Dest -Recurse -Force

$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Radar.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $Dest 'Radar.exe'
$s.WorkingDirectory = $Dest
$s.Description = 'Radar'
$s.Save()

if ($Weekly) { & (Join-Path $here 'weekly.ps1') install -Exe (Join-Path $Dest 'Radar.exe') }
Write-Host "Installed to $Dest. Open Radar from the Start menu."
