# Packs the built tray app into an MSIX for the Microsoft Store, which signs it for free on submission.
# The default identity is a test one, unsigned: install it with Developer Mode on (docs/microsoft-store.md).
# For the Store, pass the Name and Publisher from Partner Center (Product identity).
param(
    [Parameter(Mandatory)] [string]$Version,  # like 0.1.7
    [string]$Out = 'wyze-tunnel.msix',
    [string]$Name = 'WyzeTunnel.Test',
    [string]$Publisher = 'CN=Wyze Tunnel Test',
    [string]$PublisherName = 'Wyze Tunnel'
)
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path "$PSScriptRoot\..\.."
$Stage = Join-Path $env:TEMP 'wyze-tunnel-msix'
Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$Stage\Assets", "$Stage\windows" | Out-Null

Copy-Item "$Root\desktop\src-tauri\target\release\wyze-tunnel.exe" $Stage
# Same places as tauri.conf.json's resources: the app finds them next to its exe.
Copy-Item "$Root\windows\install.ps1", "$Root\windows\install-native.ps1" "$Stage\windows"

Add-Type -AssemblyName System.Drawing
$Icon = [Drawing.Image]::FromFile("$Root\desktop\src-tauri\icons\icon.png")
@{ 'Square150x150Logo' = 150; 'Square44x44Logo' = 44; 'StoreLogo' = 50 }.GetEnumerator() | ForEach-Object {
    $b = New-Object Drawing.Bitmap $Icon, $_.Value, $_.Value
    $b.Save("$Stage\Assets\$($_.Key).png", [Drawing.Imaging.ImageFormat]::Png)
    $b.Dispose()
}
$Icon.Dispose()

$Esc = { param($s) [Security.SecurityElement]::Escape($s) }
(Get-Content -Raw "$PSScriptRoot\AppxManifest.xml").
    Replace('{{NAME}}', (& $Esc $Name)).Replace('{{PUBLISHER}}', (& $Esc $Publisher)).
    Replace('{{PUBLISHER_NAME}}', (& $Esc $PublisherName)).Replace('{{VERSION}}', $Version) |
    Set-Content -Encoding utf8 "$Stage\AppxManifest.xml"

$MakeAppx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" | Sort-Object FullName | Select-Object -Last 1
& $MakeAppx.FullName pack /o /d $Stage /p $Out
if ($LASTEXITCODE) { throw "makeappx failed ($LASTEXITCODE)" }
