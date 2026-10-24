# Install or update Wyze Tunnel to run natively on Windows (no WSL). The desktop app runs this and then
# starts and supervises the bridge itself. Cameras connect through go2rtc, since the Wyze SDK is Linux-only.
# Exit codes: 0 installed or already up to date.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with its progress bar
$Dir = Join-Path $env:LOCALAPPDATA 'Wyze Tunnel'
$Native = Join-Path $Dir 'native'
$Settings = Join-Path $Dir 'wyze-tunnel.env'
$Release = 'https://github.com/itskiwi/Wyze-Tunnel/releases/latest/download'
$Bundle = 'wyze-tunnel-windows-x86_64.zip'

function Say($m) { Write-Output "`n==> $m" }

$Latest = (Invoke-RestMethod 'https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases/latest').tag_name.TrimStart('v')
$EnvFile = Join-Path $Native 'wyze-tunnel\app\.env'
$Installed = if (Test-Path $EnvFile) { (Select-String -Path $EnvFile -Pattern '^VERSION=(.*)').Matches.Groups[1].Value }

if ($Installed -ne $Latest) {
    Say "Downloading Wyze Tunnel $Latest"
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $Zip = Join-Path $env:TEMP $Bundle
    Invoke-WebRequest "$Release/$Bundle" -OutFile $Zip -UseBasicParsing
    $Sums = (Invoke-WebRequest "$Release/SHA256SUMS" -UseBasicParsing).Content
    $Want = ($Sums -split "`n" | Where-Object { $_ -match " \*?$([regex]::Escape($Bundle))$" }) -replace ' .*', ''
    if ((Get-FileHash $Zip -Algorithm SHA256).Hash -ne $Want.Trim().ToUpper()) { throw 'The download was damaged (checksum mismatch). Try again.' }

    Say 'Installing'
    $New = "$Native.new"
    Remove-Item -Recurse -Force $New -ErrorAction SilentlyContinue
    Expand-Archive $Zip $New
    Remove-Item -Recurse -Force $Native -ErrorAction SilentlyContinue
    Rename-Item $New $Native
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
Say 'Done! Wyze Tunnel is ready.'
