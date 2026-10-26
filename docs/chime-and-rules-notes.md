# Doorbell chime and Wyze rules: what the Wyze Android app does

Citations are `file:line` in the decompiled Wyze Android app (jadx `sources/`). The decompile is obfuscated and has no
resource files, so anything marked **not found** is a guess in the code, flagged here.

## Doorbell v2 (HL_DB2): two things the app calls "chime"

### 1. Ring sound and wired chime, over TUTK to the doorbell

| Setting | App call | Command | Payload |
| --- | --- | --- | --- |
| Wired chime type, ring length, switch | `ConnectControl.java:1450-1467` | 12080 (`CamCommand.java:1467`) | 3 bytes: type, switch, seconds. The app sends one non-zero field at a time |
| Read the above | `ConnectControl.java:1057` | 12082 (`CamCommand.java:1471`), reply 12083 | 3 bytes: type, switch, seconds (`CommandTreatment.java:2443-2451`) |
| Doorbell's own ring sound on/off | `ConnectControl.java:1470` | 12086 (`CamCommand.java:1479`), reply 12087 | 1 byte, 1 on, 2 off (`CameraSettingPage.java:826`) |
| Read ring sound | `ConnectControl.java:1061` | 12088 (`CamCommand.java:1483`), reply 12089 | 1 byte (`CommandTreatment.java:2465`) |

- Type values: 1 none, 2 mechanical, 3 digital (`ChimeActivity.java:508-530`, set at `:640-655`).
- Ring length: 1 to 9 seconds (`ChimeActivity.java:534-545`, set at `:669-675`).
- Set reply 12081: `data[0]` is the field changed, `data[1]` 1 means success (`CommandTreatment.java:2437-2441`, `ConnectControlHandler.java:2357-2368`).
- Used by Wyze Tunnel: `K12080SetDb2DdqParameter`, `K12082GetDb2DdqParameter`, `K12086SetDb2RingSound`, `K12088GetDb2RingSound` in `app/wyzecam/tutk/tutk_protocol.py`.
- **The "switch" byte is skipped.** Two app screens use it with opposite meaning: `ChimeActivity.java:301` and `:495` (2 when the toggle is on) against `DdqSettingsActivity.java:108` and `:188` (1 when the toggle is on). The toggle's label is in a resource file that is not in the decompile.

### 2. Wyze Chime (model `CHIME`), over the Wyze cloud

- Which chimes belong to a doorbell: devices with model `CHIME` whose parent id is the doorbell's mac (`plugin/chime/utils/DeviceDataUtils.java:19`).
- Sound, volume and a preview ring: `CloudApi.setPropertyList(handler, chimeMac, "CHIME", [{pid, pvalue}...])`
  with `P1351` volume, `P1352` ring id, `P1353` = 1 (`DDQSettingVoiceFragment.java:206-219`, `DDQRingToneAdapter.java:73-87`).
  The app always sends all three, so the chime rings once on every save. Wyze Tunnel does the same.
- Request body: `device_mac`, `device_model`, `property_list`, plus the usual sc, sv, app_ver, ts, access_token, phone_id, app_name, app_version
  (`CloudProtocol.java:2985-2998`, header fields `:128-143`). Ints, not strings, for `pvalue`.
- Ring ids and names: `BusinessInetWorker.java:44-71`. Negative ids are section headings. Ids 12 to 14 are named through `PushMessageModel.TYPE_*` constants whose values are not in the decompile; their order (Dog Bark 2, Door Close, Door Open) is assumed.
- **Not found:** the slider's range (a layout resource). Wyze Tunnel accepts 1 to 100.
- **Not found:** the URL for `CLOUD_SET_PROPERTY_LIST` and the `sv` value (both are in a properties file not in the decompile). Wyze Tunnel posts to `/app/v2/device/set_property_list` with the default sc/sv, like its existing `set_property`.
- **Not found:** reading the chime's current sound and volume. The app asks the doorbell over TUTK (`ConnectControl.java:1886`, command 11800); not implemented here, so those two controls start at defaults.

## Rules and shortcuts

- List: `v2/auto/get_auto_list` (`CloudApi.java:123`, call at `:1280`), body is only the common header (`CloudProtocol.java:2125-2131`).
- Reply: `data.auto_list[]` with `auto_id`, `auto_name`, `is_enable`, `is_hot_button`, `action_list`, `trigger_list` (`com/wyze/platformkit/model/AutoListData.java:13-28`).
- A shortcut is an automation created with `is_hot_button` 1 (`defpackage/hin.java:241`); ordinary rules use 0 (`plugin/chime/http/BusinessInetWorker.java:165`). Inferred from this, not from the Rules tab code, which is obfuscated.
- Run: `v2/auto/run_auto` with `auto_id` (`CloudApi.java:125`, call at `:1540-1542`, body `CloudProtocol.java:2583-2594`).
- Signing: none beyond the common header (`access_token` in the body); no `signature2` on these calls.
- **Not found:** the `sv` values and base URL for these calls. Wyze Tunnel uses the default sc/sv and the same `https://api.wyzecam.com/app` base as its existing `run_action`.

## Not touched by the app code we found

- Editing or enabling rules, and rule schedules (`DbScheduleActivity`): out of scope.
- The Doorbell Pro (GW_BE1, Meari/Gwell) has its own chime screens (`com/meari/plugin/basicbatterydb/page/setting/DVDChimeSettingActivity.java`) and is not covered.
