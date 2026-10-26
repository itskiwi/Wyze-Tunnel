# Lock activity and thermostat history: endpoint notes

Source: the decompiled Wyze Android app (v3.24). Paths are relative to its `sources/` folder. Everything here is read-only.

## Lock (YD.LO1, Ford, yd-saas-toc.wyzecam.com, signed like the existing lock info call)

| What | Endpoint | Evidence |
| --- | --- | --- |
| Recent activity | `GET /openapi/v1/safety/family_record?uuid&offset&limit[&begin&end]` | `com/yunding/commonbiz/constant/HttpUrl.java:34` (path, `/openapi` prefix at :24); params `com/yunding/ford/manager/impl/NetLockManager.java:53-90`; caller `com/yunding/commonbiz/helper/HistoryHelper.java:42-48` |
| Keypad codes | `GET /openapi/lock/v1/pwd?uuid&detail=1` | `com/yunding/ford/constant/FordHttpUrl.java:37`; `com/yunding/ford/manager/impl/KeyManager.java:505-532`; response `passwords` in `com/yunding/commonbiz/entity/LockKeyEntity.java:9` |

Record fields: `eventid`, `time` (ms, `HomeListAdapter.java:195-197`), `detail.source`, `detail.source_name` (`com/yunding/commonbiz/entity/FamilyRecord.java`).
Event ids shown by the app: 2203 unlock and 2216 lock (`HomeListAdapter.java:336-354`), 2214 door opened and 2215 door closed (`:386-388`).
Code fields: `name`, `pwd_state` (4 = expired, `EntryCodeListAdapter.java:112`), `permission.status/begin/end` (3 = one-time, 4 = ends at a time, `:117`, `UserInfoActivity.java:579`), `period_info.valid_days/begin/end` (`com/yunding/ydbleapi/bean/LockPasswordInfo.java:27-39`, `PeriodicityInfo.java`).

The code digits are in `LockPasswordInfo.pwdValue` (`:36`). `devices.lock_log` copies only an allow-list of fields, so digits, emails and avatars are dropped before anything leaves devices.py.

Not used: `/lock/v1/history/operations/synchronize` and friends (`defpackage/qpz.java:19,51`) upload history from the lock to Wyze (writes). `/app/v2/lock/get_notification` (`defpackage/x6y.java:35`) is the app's notification setting, not a log.

## Thermostat (CO_EA1) and room sensors (CO_TH1)

`get_iot_history` (`com/wyze/sensor/SensorApi.java:43`) and `work_statistics` (`:53`) exist as constants, but nothing in the decompile calls them for temperature, humidity or runtime. The only `get_iot_history` call (`com/wyze/platformkit/firmwareupdate/iot/manager/WpkFirmwareUpdateManager.java:58`) asks for firmware upgrade history, and the thermostat screens are not in this APK. The request shape for readings is unconfirmed, so it is not called.

Instead the bridge keeps its own history: each Devices refresh (and a background check every 3 minutes once there is history or the lock alert is on) saves one reading per 5 minutes to `device_history.json` for 7 days. Graphs start from when the bridge first saw the device. Heating and cooling minutes come from `working_state` containing "heat" or "cool"; those values are unverified (no value list in the decompile).

## Unverified

- Meaning of `detail.source` (keypad, app, manual, auto): the strings are in resources not present in the decompile, so the page shows the number.
- `permission.status` values other than 3 and 4, and `period_info.type`.
- `working_state` values.
- The `family_record` and `pwd` calls reuse the signing already used for `lock/v1/info`; no live call was made.
