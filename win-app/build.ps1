# Builds P2 Radar for Windows and installs it for the current user.
#   .\build.ps1                build, install to %LOCALAPPDATA%\Programs\P2 Radar, add a Start menu shortcut
#   .\build.ps1 -Weekly        also schedule the weekly check (Mondays 10:00)
#   .\build.ps1 -NoInstall     build only (into .\publish)
# Needs the .NET 8 SDK (https://dotnet.microsoft.com/download). Python is downloaded and bundled.
param(
  [switch]$Weekly,
  [switch]$NoInstall,
  [string]$Dest = (Join-Path $env:LOCALAPPDATA 'Programs\P2 Radar')
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

# A private copy of Python, so nobody has to install one.
Write-Host '-> python'
$pyVersion = '3.13.18'; $pyRelease = '20261026'
$pySha = '7369f7a35f261222a6ac8b3a87c1c88eeebee8af945d85346202591a64038dd3'
$pyName = "cpython-$pyVersion+$pyRelease-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$pyUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$pyRelease/$($pyName -replace '\+', '%2B')"
$pyCache = Join-Path $here '.python-cache'
New-Item -ItemType Directory -Force $pyCache | Out-Null
$pyFile = Join-Path $pyCache $pyName
if (-not (Test-Path $pyFile)) { Invoke-WebRequest $pyUrl -OutFile $pyFile }
if ((Get-FileHash $pyFile -Algorithm SHA256).Hash -ne $pySha) { Remove-Item $pyFile; throw 'Python download failed its checksum' }
tar -xzf $pyFile -C $out
if ($LASTEXITCODE -ne 0) { throw 'Could not unpack Python' }
$py = Join-Path $out 'python'
foreach ($p in 'include', 'Scripts', 'tcl', 'Lib\test', 'Lib\idlelib', 'Lib\tkinter', 'Lib\turtledemo', 'Lib\ensurepip', 'Lib\pydoc_data', 'Lib\venv') {
  $target = Join-Path $py $p
  if (Test-Path $target) { Remove-Item $target -Recurse -Force }
}
Get-ChildItem (Join-Path $py 'Lib\site-packages') -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem (Join-Path $py 'DLLs') -Filter '*t*' -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^(_tkinter|tcl|tk|_test)' } | Remove-Item -Force
& (Join-Path $py 'python.exe') -m compileall -q --invalidation-mode unchecked-hash (Join-Path $py 'Lib')
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the Python standard library' }

if ($NoInstall) { Write-Host "Built $out"; return }

Write-Host '-> install'
# 'Radar' is the app's name from before it became P2 Radar
Get-Process 'P2 Radar', Radar -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force $Dest | Out-Null
Copy-Item (Join-Path $out '*') $Dest -Recurse -Force

$programs = [Environment]::GetFolderPath('Programs')
Remove-Item (Join-Path $programs 'Radar.lnk') -ErrorAction SilentlyContinue
$lnk = Join-Path $programs 'P2 Radar.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $Dest 'P2 Radar.exe'
$s.WorkingDirectory = $Dest
$s.Description = 'P2 Radar'
$s.Save()

if ($Weekly) { & (Join-Path $here 'weekly.ps1') install -Exe (Join-Path $Dest 'P2 Radar.exe') }
Write-Host "Installed to $Dest. Open P2 Radar from the Start menu."
