# Wyze cloud history: what the app proves

Paths are relative to a jadx decompile of the Wyze Android app (v3.24) unless noted. Asset and string files come from unpacking
`com.hualai.apk` (base) and its `res/values/strings.xml`.

## 1. Lock event and source labels (done)

- Strings live in the base resources.arsc: `yunding_lock_history_event_id_{2201,2203,2214,2215,2216,2218,2221-2228}` and
  `yunding_lock_history_source_{1..9,12,102}` (`values/strings.xml`). Ids 2217 has no string.
- Which string is shown: `com/yunding/commonbiz/ui/adapter/HomeListAdapter.java:163-199` (`getEventIdString`, `getSourceString`,
  `getTitleString`) and `:336-409` (2203/2216 show the source, the others only the title). Bolt and gun safe label source 2 "Code"
  (`:177-181`); the Wyze Lock label is "Keypad". 2218 appends `yunding_open_alarm_time1[left_open_time - 2]` (`:372`; array in `values/arrays.xml`).
- Implemented in `devices.py` (`LOCK_EVENTS`, `LOCK_SOURCES`, `_lock_event`). Unknown ids still show "Event N" / "source N".

## 2. Thermostat temperature and runtime history (not implemented)

- `SensorApi.java:43,53` (`URL_GET_IOT_HISTORY`, `WORK_STATISTICS`) and the sun/pluto copies (`com/wyze/sun/DeviceApi.java:56,84`,
  `com/wyze/pluto/network/DeviceApi.java:117,134`) are never called anywhere in the base.
- The only caller of `get_iot_history` is `com/wyze/platformkit/firmwareupdate/iot/manager/WpkFirmwareUpdateManager.java:58`, with
  `action=upgrade`: firmware upgrade results, not temperatures.
- The thermostat UI is the `com.wyze.earth` plugin (`EarthCenter`, `EarthUsageActivity`): named in
  `com/hualai/home/application/SHApplication.java:424` and `defpackage/dqx.java:23-30` and mapped to `WyzeThermostatUpdate` in
  `WyzePluginUpgradeFactory.java:156`, but no `com/wyze/earth` class is defined in any of the 36 dex files (only name references) and none of the four
  `*PluginContainer.apk` splits (Lockwood, Saturn, Scale, Watch/Band) contain it. `SensorHomeActivity.toChartActivity()` is an empty stub.
  So the request shape (and whether `work_statistics` is the runtime report) is not provable here; the bridge keeps its own `device_history.json`.

## 3. Plug usage history (done: time on, not energy)

- `assets/WLPPO_URL`: `URL_WLPP1_PLUG_USAGE_RECORD_LIST=v2/plug/usage_record_list`; `assets/WLPPO_SV`: `SV_WLPP1_PLUG_USAGE_RECORD_LIST`.
- Body: `com/HLApi/CloudAPI/CloudProtocol.java:2200-2212` (`date_begin`, `date_end`, `device_mac`, `phone_system_type` plus the standard head
  from `composeHead`, `:128-143`). Window and parsing: `defpackage/duz.java:78-90,100-170` (indoor plug) and `defpackage/tr10.java:93-100` (outdoor).
  The outdoor plug asks with the parent plug mac: `com/hualai/wlppo/DeviceMainPage.java:329`, `e2.java:398`.
- Reply: `usage_record_list`, rows of `{date_ts, data: "[24 numbers]"}`; the app clamps each hour to 60 and shows hours on
  (`DateUsage.useHour`, `duz.java:164-200`). That is minutes on per hour, not watt-hours. No energy reading request exists in the plug modules.
- Implemented in `devices.plug_usage` and `GET /api/devices/usage` (read-only, `auth_required`).
- Not verified live: the head reuses the bridge's default `sc`/app fields with the plug's `sv`, as `set_bulb` does.

## Searched and not found

Lock/thermostat/plug modules other than the above are not in the provided splits; no plugin download URL builder exists in the base
(the earth plugin is simply absent from the xapk set).
