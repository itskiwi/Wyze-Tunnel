# Microsoft Store and winget

The Microsoft Store signs apps for free, so a Store install never shows the blue "Windows protected your PC" screen. It is the
free alternative to [Artifact Signing](windows-signing.md). Store apps also update themselves, and once the app is in the Store,
`winget install "Wyze Tunnel" --source msstore` works too, with no extra setup.

`.github/workflows/desktop.yml` builds the Store package (an MSIX) on every Windows build and checks that it installs. Find it
under the run's artifacts as **store-msix**. It is never attached to GitHub releases.

## How the Store version differs

- **Same files as the regular installer.** The package turns off Windows' file and registry redirection for packaged apps
  (the `unvirtualizedResources` capability), so the bridge still lives in `%LOCALAPPDATA%\Wyze Tunnel`. The Windows Firewall
  rules that the "Fix phone and camera access" prompt adds point at those real paths, and the bridge's own updater keeps
  working.
- **Start at sign-in** comes from the package's startup task instead of the Run key. Users can turn it off in Settings, Apps,
  Startup.
- **App updates** come from the Store, so the tray stops pointing at GitHub releases. Windows restarts the tray after a Store
  update.

## Trying the test package (Windows 11)

The CI's `wyze-tunnel-<version>-test.msix` is unsigned on purpose. Uninstall the regular Wyze Tunnel app first, so that two trays
don't run. Then, in PowerShell:

```powershell
Add-AppxPackage -AllowUnsigned .\wyze-tunnel-0.1.7-test.msix
```

Remove it in Settings, Apps, or with `Get-AppxPackage WyzeTunnel.Test | Remove-AppxPackage`. Your settings and cameras stay in
`%LOCALAPPDATA%\Wyze Tunnel`, as with the regular app.

## Submitting to the Store (owner steps)

1. Sign up as an individual developer at [storedeveloper.microsoft.com](https://storedeveloper.microsoft.com). This is free.
2. In Partner Center, choose **Apps and games**, then **New product**, then **MSIX or PWA app**, and reserve the name
   "Wyze Tunnel".
3. Open **Product management**, then **Product identity**. In GitHub, under **Settings**, **Secrets and variables**,
   **Actions**, **Variables**, add `STORE_NAME` (Package/Identity/Name), `STORE_PUBLISHER` (Package/Identity/Publisher) and
   `STORE_PUBLISHER_NAME` (Package/Properties/PublisherDisplayName). These are public values, not secrets.
4. Run **Actions**, **Desktop apps**, **Run workflow**. The **store-msix** artifact then also has
   `wyze-tunnel-<version>-store.msix`. Upload that file in the submission's **Packages** step.
5. Under **Submission options**, explain the two restricted capabilities:
   - `runFullTrust`: a desktop tray app that runs a local video server for the user's own cameras.
   - `unvirtualizedResources`: the app adds Windows Firewall rules for the programs it installs in `%LOCALAPPDATA%`, and those
     rules need the real file paths.

A certification risk to know about: the Store's policy 10.2.2 limits apps that download code. The tray downloads the bridge from
GitHub releases on first setup and then updates it on its own. If certification rejects that, the fix is to put the bridge
inside the package and let Store updates carry it.
