# Install or update Wyze Tunnel to run natively on Windows (no WSL). The desktop app runs this and then
# starts and supervises the bridge itself. Cameras connect through go2rtc, since the Wyze SDK is Linux-only.
# Exit codes: 0 installed or already up to date.
# -CheckFirewall only checks the firewall rules (exit 2: phones or cameras are blocked); -FirewallOnly only fixes them.
param([switch]$CheckFirewall, [switch]$FirewallOnly)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with its progress bar
$Dir = Join-Path $env:LOCALAPPDATA 'Wyze Tunnel'
$Native = Join-Path $Dir 'native'
$Settings = Join-Path $Dir 'wyze-tunnel.env'
$Release = 'https://github.com/itskiwi/Wyze-Tunnel/releases/latest/download'
$Bundle = 'wyze-tunnel-windows-x86_64.zip'

function Say($m) { Write-Output "`n==> $m" }

# Phones reach the web app (python.exe), cameras answer go2rtc and wyze-ioctl over UDP, other devices reach MediaMTX,
# and phones away from home connect straight to wyze-away (embedded Tailscale), so all need inbound firewall rules.
# One Windows permission prompt, only when a rule is missing, too open, or a Block rule (from a canceled firewall prompt) exists.
# Only Wyze Tunnel's own programs are touched (approved by the project owner, 2026-10-24).
$Exes = 'go2rtc.exe', 'mediamtx.exe', 'wyze-ioctl.exe', 'wyze-away.exe', 'python\python.exe' | ForEach-Object { Join-Path $Native "wyze-tunnel\$_" }
# The web page and video server only take visitors from home networks and Tailscale (100.64.0.0/10), never the internet.
# The other three only get answers to their own calls out (cameras, Wyze's cloud, Tailscale peers), so they stay open.
$Reachable = 'LocalSubnet', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7', 'fe80::/10'
function Get-Scope($exe) { if ((Split-Path $exe -Leaf) -in 'python.exe', 'mediamtx.exe') { $Reachable } else { @('Any') } }
function Get-Blocked {
    $Exes | Where-Object {
        $rules = Get-NetFirewallApplicationFilter -Program $_ -ErrorAction SilentlyContinue | Get-NetFirewallRule -ErrorAction SilentlyContinue |
            Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' }
        $open = $rules | Where-Object Action -eq 'Allow'
        # An allow rule open to any address (Windows' own prompt makes those) counts as needing a fix too.
        $tooOpen = (Get-Scope $_) -notcontains 'Any' -and ($open | Get-NetFirewallAddressFilter | Where-Object { $_.RemoteAddress -contains 'Any' })
        -not $open -or ($rules | Where-Object Action -eq 'Block') -or $tooOpen
    }
}
function Fix-Firewall {
    if (-not (Get-Blocked)) { return }
    # The tray asks for the prompt itself: one asked from its windowless installer opens unseen and times out.
    if ($env:WT_TRAY) { Write-Output 'Phones and cameras need a firewall change. Wyze Tunnel will ask for it (or use Fix phone and camera access in its tray menu).'; return }
    Say 'Letting phones and cameras through Windows Firewall (click Yes on the Windows permission prompt)'
    $Fix = ($Exes -replace "'", "''" | ForEach-Object {
        "Get-NetFirewallApplicationFilter -Program '$_' -ErrorAction SilentlyContinue | Get-NetFirewallRule | Where-Object Direction -eq Inbound | Remove-NetFirewallRule; " +
        "New-NetFirewallRule -DisplayName 'Wyze Tunnel ($(Split-Path $_ -Leaf))' -Direction Inbound -Action Allow -Program '$_' -Profile Any -RemoteAddress '$((Get-Scope $_) -join "','")' | Out-Null"
    }) -join '; '
    try {
        $Encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Fix))  # no quoting to get wrong
        $p = Start-Process powershell.exe -ArgumentList '-NoProfile', '-EncodedCommand', $Encoded -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if ($p.ExitCode) { Write-Output "The firewall change didn't finish (error $($p.ExitCode)). Phones and cameras on this network may not connect." }
    } catch {
        Write-Output "Skipped the firewall change. Phones and cameras on this network may not connect until you click Set up again and choose Yes. ($($_.Exception.Message))"
    }
}
if ($CheckFirewall) { exit $(if (Get-Blocked) { 2 } else { 0 }) }
if ($FirewallOnly) { Fix-Firewall; exit }

$Latest = (Invoke-RestMethod 'https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases/latest').tag_name.TrimStart('v')
$EnvFile = Join-Path $Native 'wyze-tunnel\app\.env'
$Installed = if (Test-Path $EnvFile) { (Select-String -Path $EnvFile -Pattern '^VERSION=(.*)').Matches.Groups[1].Value }

if ($Installed -ne $Latest) {
    Say "Downloading Wyze Tunnel $Latest"
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $Zip = Join-Path $env:TEMP $Bundle
    Invoke-WebRequest "$Release/$Bundle" -OutFile $Zip -UseBasicParsing
    # Via a file: GitHub serves SHA256SUMS as octet-stream, so PowerShell 5.1's .Content would be bytes.
    $SumsFile = Join-Path $env:TEMP 'wyze-tunnel-SHA256SUMS'
    Invoke-WebRequest "$Release/SHA256SUMS" -OutFile $SumsFile -UseBasicParsing
    $Sums = Get-Content -Raw $SumsFile
    Remove-Item $SumsFile
    $Want = ($Sums -split "`n" | Where-Object { $_ -match " \*?$([regex]::Escape($Bundle))$" }) -replace ' .*', ''
    if ((Get-FileHash $Zip -Algorithm SHA256).Hash -ne $Want.Trim().ToUpper()) { throw 'The download was damaged (checksum mismatch). Try again.' }

    Say 'Installing'
    $New = "$Native.new"
    $Old = "$Native.old"
    Remove-Item -Recurse -Force $New, $Old -ErrorAction SilentlyContinue
    Expand-Archive $Zip $New
    # A bridge the desktop app no longer tracks (say, one left running across an app update) holds files open.
    Get-Process | Where-Object { $_.Path -like "$Native\*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 2
    # Swap whole folders: if a file is still locked the rename fails and the old install stays intact,
    # instead of a delete that removes half the files.
    if (Test-Path $Native) { Rename-Item $Native $Old }
    Rename-Item $New $Native
    Remove-Item -Recurse -Force $Old -ErrorAction SilentlyContinue  # retried on the next update if still locked
    Remove-Item $Zip
} else {
    Say "Wyze Tunnel $Latest is up to date"
}

if (-not (Test-Path $Settings)) {
    # Same defaults as the Linux installer: a random web password, shown once in the app.
    $Bytes = New-Object byte[] 12
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
    $Password = [Convert]::ToBase64String($Bytes).Replace('+', '-').Replace('/', '_')
    Set-Content -Path $Settings -Encoding ascii -Value @(
        '# Wyze Tunnel settings. Every option from the README works here as NAME=value.',
        '# Quit and reopen Wyze Tunnel after editing.',
        'WB_AUTH=True',
        "WB_PASSWORD=$Password"
    )
}
Fix-Firewall
Say 'Done! Wyze Tunnel is ready.'
