# Install or update Wyze Tunnel on Windows. The desktop app runs this; it also works on its own:
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
# The Wyze camera library only exists for Linux, so this runs the native Linux
# install inside a WSL2 distro named WyzeTunnel. The desktop app keeps that distro running.
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'  # otherwise wsl.exe prints UTF-16
$Distro = 'WyzeTunnel'
$Ref = if ($env:WT_REF) { $env:WT_REF } else { 'main' }
$Dir = Join-Path $env:LOCALAPPDATA 'Wyze Tunnel'

function Say($m) { Write-Output "`n==> $m" }
function Wsl { & wsl.exe @args; if ($LASTEXITCODE) { throw "wsl $args failed ($LASTEXITCODE)" } }

# Through cmd: PowerShell 5.1 turns redirected native stderr into a terminating error under 'Stop'.
cmd.exe /c 'wsl.exe --status >nul 2>&1'
if ($LASTEXITCODE) {
    Say 'Turning on WSL (Windows will ask for permission)'
    wsl.exe --install --no-distribution
    if ($LASTEXITCODE) { throw "Turning on WSL didn't finish ($LASTEXITCODE). Approve the Windows permission prompt and try again." }
    Write-Output 'Restart your PC, then open Wyze Tunnel again.'
    exit 3
}

# Older inbox wsl.exe ignores WSL_UTF8 and prints UTF-16, hence the NUL stripping.
$Distros = wsl.exe -l -q | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($Distros -notcontains $Distro) {
    Say 'Updating WSL'
    wsl.exe --update  # --name and --location need WSL 2.4.4+
    Say 'Creating the Wyze Tunnel Linux system (one time, takes a few minutes)'
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Wsl --install Debian --name $Distro --location (Join-Path $Dir 'wsl') --no-launch
    # systemd runs the service; root as default user skips the Debian first-run user prompt.
    Wsl -d $Distro -u root -- sh -c "printf '[boot]\nsystemd=true\n[user]\ndefault=root\n' > /etc/wsl.conf"
    Wsl --terminate $Distro
}

Say 'Installing Wyze Tunnel inside WSL'
Wsl -d $Distro -u root -- bash -euo pipefail -c "command -v curl >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null; }; curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/$Ref/native/install.sh | WT_REF=$env:WT_REF bash >/dev/null"

$Password = wsl.exe -d $Distro -u root --exec sed -n 's/^WB_PASSWORD=//p' /etc/wyze-tunnel/wyze-tunnel.env
Say 'Done! Wyze Tunnel is running.'
Write-Output '  Open:  http://localhost:5000'
Write-Output '  Sign in with your Wyze email, password, API ID and API key'
Write-Output '  (get the API ID and key at https://developer-api-console.wyze.com/#/apikey/view).'
Write-Output "  Then log in as wbadmin with the password: $Password"
Write-Output '  RTSP:  rtsp://localhost:8554/<camera-name>'
