# Camera extras: where each command comes from

Paths are under the decompiled Wyze Android app (`jadx/sources/com/`), `HLApi` = `HLApi/CameraAPI`.

## Built

| Feature | Command | Evidence |
| --- | --- | --- |
| Floodlight v2 (HL_CFL2) read | K10788 -> reply 10789 JSON `floodlightInfo` | `HLApi/protocol/CamCommand.java:1342`, `protocol/CommandTreatment.java:80-90,1987` |
| Floodlight v2 write | K10790, JSON `{"floodlightInfo": {..., "protocolVersion": "1"}}`, only the changed keys | `CamCommand.java:1346`, `connection/ConnectControl.java:1564-1573` |
| Brightness `fl_brightness` 1-100 | key `brightness` (string), sent alone | `hualai/cam_v3/.../floodlightV2/FloodLightV2Page.java:379-387`, slider min 1 max 100 at `:424-425` |
| Motion light on/off `fl_motion` | `motionSwitch` "1" on, "2" off, sent together with the whole `motion` block | `floodlightV2/MotionSettingPage.java:89,500-507`; read side `HLApi/Obj/IntegratedFloodLightInfo.java:537-538` |
| Light duration `fl_duration` (seconds) | `motion.durations` (string), whole `motion` block (`model`, `dispose`, `durations`, `schedule`) | `MotionSettingPage.java:296-301,489-507`; `IntegratedFloodLightInfo.java:280-300` |
| Doorbell v2 (HL_DB2) WDR read/write `wdr` | K12090 get, K12092 set (1 on, 2 off); reply byte 0 | `CamCommand.java:1487-1493`, `CameraSettingPage.java:518-535,1180`, `CommandTreatment.java:2471-2482` |
| Doorbell v2 battery level `db2_battery` (read only, API only) | K12084; reply 12085 byte 0 | `CamCommand.java:1475`, `CommandTreatment.java:2453-2457`, `live/db2/ddq/DdqSettingsActivity.java:80-88,126` |

The three `fl_*` commands read the camera's current settings first (K10788) so one change keeps the rest of the motion block, as the app does.

## Already in the repo (nothing added)

- Pan home: `reset_rotation` = K11004 (`CamCommand.java:968`, `ConnectControl.java:1324`) and saved positions via `cruise_point`. Both already have buttons in `live.html`.
- Doorbell quick replies: `quick_response` = K11635 with a 1-byte reply id (`CamCommand.java:1429`, `ResponsePop.java:121`). Already in the table and UI. A list command is not worth adding: the ids 1-3 are fixed in the existing UI, and the app shows the text from its own string resources, which the decompile output here doesn't include.
- Doorbell battery for older models: `battery_usage` = K10448.

## Skipped

- Floodlight (v1, Cam v3 + floodlight accessory) mode/brightness/timer: K12052/K12054 (`CamCommand.java:1437,1444`). The mode needs a "trigger" bitmask read back from K12050, and brightness/duration go through a 13-byte K12054 struct whose field order I couldn't confirm; newer firmware uses a cloud call instead (`CamV3FloodLightPage.java:725-740`). Not worth guessing on hardware.
- Floodlight v2 sensitivity: the app sets it with a cloud property (`FloodlightDetectionSettingPage.java:591-601`), not TUTK.
- Floodlight v2 light on/off via K10790 `switch` (`CamLiveMainPage.java:3537-3547`): the existing `floodlight` command (K12060) covers on/off.

## Not verified

- Duration values: the app picks from a resource list (`cam_interval_time_value_array`) that wasn't in the decompile. The API accepts 1-3600 seconds, which is a guess.
- Reply encodings (K10788 JSON, K12084/K12090 one byte) come from the app's parsing code, not from a camera.
- Floodlight v2 only applies to HL_CFL2, WDR/battery to HL_DB2; Floodlight Pro (LD_CFP) and others may differ.

## Guards

`wdr`, `fl_motion`, `fl_duration`, `fl_brightness` with a value are refused with 403 for guests and family logins (`wyze_commands.OWNER_CMDS`, checked in `/api/<cam>/<cmd>` and `/api/v1/.../commands/<cmd>`). Reading `wdr`/`db2_battery` only needs a normal login.
