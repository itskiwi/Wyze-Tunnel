# Firmware update: what the Wyze Android app does

Paths are in the decompiled app (`jadx/sources/`). Nothing here was run against a real camera or account.

## Check ("Firmware update available")

- `POST https://wyze-upgrade-service.wyzecam.com/app/v2/upgrade/get_upgrade_firmware`
  (`com/wyze/platformkit/firmwareupdate/http/WpkUpdatePlatform.java:152-154`; host `com/wyze/platformkit/config/ServiceConfig.java:12`).
- JSON body: `device_id` (MAC), `device_model`, `current_ver`, `testcode` ("Official Version" for release builds,
  `WyzeServiceUtils.java:28`, `AppConfig.java:140-141`), and `hardware_ver` when the app knows it.
- Signed ("dynamic signature"): header `Signature2` over the body string, with `appid`, `access_token`, `phoneid`
  headers (`com/wyze/platformkit/network/WpkWyzeExService.java:229-262`). Wyze Tunnel already has this scheme as
  `sign_payload()` for the v4 calls, so the check reuses it.
- Reply: `data.firmware_ver` is the offered version; the app shows "update available" when it is newer than the camera's
  (`com/hualai/cam_v3/camera/activity/CamV3InfoActivity.java:236-240`). No `data` means nothing offered (`:229-232`).
  `data.id` is the target id used by install (`WYZEFirmwareDetail.java:9`).

## Install

- `POST https://wyze-upgrade-service.wyzecam.com/app/v2/upgrade/begin_upgrade`
  (`WpkUpdatePlatform.java:85-96`), same signing. Body: `id` (from the check), `deviceId`, `deviceModel`, `currentVer`,
  `targetVer`, `hardwareVer` (only when known), `testCode`.
- Called from `WpkIotUpgradePresenter.java:417` and `WyzeUpgradeMethodManger.java:117-123`. The app treats reply code
  "1" or "2" as started (`WpkIotUpgradePresenter.java:~425`). The camera then downloads and flashes it itself.
- No TUTK command is involved. There is a second, older route (`/app/v2/auto/run_action` with `action_key: "upgrade"`,
  `WyzeUpgradeMethodManger.java:171-195`, `CamV3InfoActivity.java:373`) that sends the firmware url and md5. It is not used.

## Not verified

- The app id the app signs with for these calls: Cam V3 passes `wcap_cc2bfc6e1c4c6a7a` (`CamV3InfoActivity.java:731`),
  the batch screen passes `9319141212m2ik` (`WyzeUpgradeMethodManger.java:123`). Wyze Tunnel only has the secret for
  `9319141212m2ik`, so that is what it uses. Wyze may reject it.
- `hardware_ver` is not in the cloud camera list, so it is left out. Wyze may then offer nothing for some models.
- The signing algorithm itself is in native code (`WpkSecurity.getSignature`); the Python `sign_msg` is assumed to match.
- Code "2" (the app counts it as started) shows up as an error here, because `_validate_resp` only accepts 0 and 1.
