<#
.SYNOPSIS
  Installs ProxySQL Web UI on Windows (Windows 10/11, Windows Server 2019+).

.DESCRIPTION
  - creates a virtualenv in <InstallDir>\venv and installs dependencies (Flask, PyMySQL, waitress)
  - writes <InstallDir>\config.ini (only if missing) with a random session secret
  - creates the first web UI account "admin" with a random password (printed once)
  - registers a scheduled task "ProxySQL Web UI" that starts the server at boot (runs as SYSTEM)
  - optionally opens the port in Windows Firewall (-OpenFirewall)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -AdminHost 10.0.0.5 -AdminUser radmin -AdminPassword S3cret
#>
param(
  [string]$InstallDir = "C:\ProxySQL-WebUI",
  [string]$BindHost = "127.0.0.1",
  [int]$Port = 8080,
  [string]$Python = "python",
  [string]$AdminHost = "127.0.0.1",
  [int]$AdminPort = 6032,
  [string]$AdminUser = "radmin",
  [string]$AdminPassword = "radmin",
  [switch]$OpenFirewall,
  [switch]$NoTask
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this script from an elevated PowerShell (Run as Administrator)."
}

$src = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
& $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.9+ is required (install it from python.org and tick 'Add to PATH')." }

Write-Host "==> Installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir, "$InstallDir\data" | Out-Null
if (Test-Path "$InstallDir\app") { Remove-Item -Recurse -Force "$InstallDir\app" }
Copy-Item -Recurse "$src\app" "$InstallDir\app"
Copy-Item "$src\serve.py", "$src\wsgi.py", "$src\requirements.txt" $InstallDir -Force

if (-not (Test-Path "$InstallDir\venv\Scripts\python.exe")) {
  Write-Host "==> Creating virtualenv"
  & $Python -m venv "$InstallDir\venv"
}
Write-Host "==> Installing Python dependencies"
& "$InstallDir\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$InstallDir\venv\Scripts\python.exe" -m pip install --quiet -r "$InstallDir\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$cfg = "$InstallDir\config.ini"
if (-not (Test-Path $cfg)) {
  Write-Host "==> Writing $cfg"
  $secret = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
  @"
[webui]
secret_key = $secret
data_dir = $InstallDir\data
session_lifetime_minutes = 480
# waitress serves plain HTTP: keep false unless an HTTPS reverse proxy (IIS/nginx) is in front
secure_cookies = false
max_login_failures = 5
lockout_minutes = 15
behind_proxy = false

[service]
# systemd service control is Linux-only
enabled = false

[instance:proxysql1]
label = ProxySQL node 1
host = $AdminHost
port = $AdminPort
user = $AdminUser
password = $AdminPassword
"@ | Set-Content -Encoding ascii $cfg
  # config contains ProxySQL admin credentials: restrict to Administrators and SYSTEM
  icacls $cfg /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
} else {
  Write-Host "==> Keeping existing $cfg"
}

$env:PROXYSQL_WEBUI_CONFIG = $cfg
Push-Location $InstallDir
$users = & "$InstallDir\venv\Scripts\python.exe" -m app.cli list-users
$initial = $null
if (-not $users) {
  $initial = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 16 | ForEach-Object { [char]$_ })
  & "$InstallDir\venv\Scripts\python.exe" -m app.cli create-user admin --role admin --password $initial | Out-Null
}
Pop-Location

# start script used by the scheduled task
@"
@echo off
set PROXYSQL_WEBUI_CONFIG=$cfg
cd /d "$InstallDir"
"$InstallDir\venv\Scripts\python.exe" serve.py --host $BindHost --port $Port >> "$InstallDir\data\webui.log" 2>&1
"@ | Set-Content -Encoding ascii "$InstallDir\start-webui.cmd"

if (-not $NoTask) {
  Write-Host "==> Registering scheduled task 'ProxySQL Web UI' (starts at boot)"
  $action = New-ScheduledTaskAction -Execute "$InstallDir\start-webui.cmd"
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName "ProxySQL Web UI" -Action $action -Trigger $trigger -Settings $settings `
    -User "SYSTEM" -RunLevel Highest -Force | Out-Null
  Stop-ScheduledTask -TaskName "ProxySQL Web UI" -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName "ProxySQL Web UI"
}

if ($OpenFirewall) {
  New-NetFirewallRule -DisplayName "ProxySQL Web UI" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null
}

Write-Host ""
Write-Host "ProxySQL Web UI installed."
Write-Host "  URL:    http://$($BindHost):$Port/"
if ($initial) { Write-Host "  Login:  admin / $initial   (change it under 'My Account')" }
Write-Host "  Config: $cfg"
Write-Host "  Log:    $InstallDir\data\webui.log"
