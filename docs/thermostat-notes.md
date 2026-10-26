# Thermostat (CO_EA1): what the Wyze app does

Paths are relative to a jadx decompile of the `com.wyze.earth` plugin (the on-demand split `split_CocleanPluginEarthAndroidContainer.apk`,
app 3.24). Every call goes to `https://wyze-earth-service.wyzecam.com/plugin/earth` with Signature2 (see `devices.earth_get`).
Values are strings.

## Used by Wyze Tunnel

| Feature | Props | Source |
| --- | --- | --- |
| Fan | `fan_mode` `auto`/`on`; `fancirc_time` 0-30 in steps of 5 (minimum fan minutes per hour) | `common/widget/EarthHomeControls.java:215-224`, `activity/setting/EarthSettingSystemActivity.java:240,920` |
| Presets | `current_scenario` `home`/`away`/`sleep`, sent with that preset's `heat_sp`/`cool_sp` from the `config_scenario` JSON | `activity/home/EarthMainActivity.java:1056-1066` |
| Hold | on: `dev_hold=1`, `dev_holdtime` epoch seconds (0 until resumed); off: `dev_hold=0, asw_hold=0, dev_holdtime=0` | `EarthHomeControls.java:230-235,397-405` |
| Emergency heat | `emheat` 1/0 | `EarthSettingSystemActivity.java:1104` |
| Room sensors | `sensor_using` JSON keyed by sensor mac: `auto_up`/`auto_down`/`manual_up`/`manual_down`; `avg_temper`, `avg_humid` | `activity/home/EarthSensorsActivity.java:365-366,436-456` |
| Filters | `filter_N` (0 off), `filtercnt_N` run seconds; GET `device_info?keys=filters` gives name, size, life in months. Used % = cnt / (life x 43200 x 60); replace at 95%. Reset `filter_N=2` (firmware 1.1.2+) or `1` | `activity/filters/EarthFilterActivity.java:71,129-180,320-323` |

Checked live (read only): every key above comes back; `dev_holdtime` and `asw_hold` are absent until a hold is set. The room sensor macs in
the device list are the `device_id`s in `get_sub_device` and the keys of `sensor_using`.

## Not used yet

- Schedules: `get_iot_prop keys=query_schedule` gives a timestamp; `query_data?purpose=schedule&last_time=<it>&count=1` returned only
  `create_time` and `ttl_time` live, no schedule. The app writes with `send_data` (`activity/schedule/ScheduleFragment.java:454-481`);
  its body encoding is unconfirmed.
- Limits: `secure_temp_low` 35-50, `secure_temp_high` 80-105; humidity `phi_l` 20-80, `phi_h` 40-100; `save_comfort_balance` 1-5 with
  `trigger_off_val`; `kid_lock`, `air_wave`, `fan_delay_time`, calibration, reboot (`set_iot_action cmd=reboot`).
- Insight cards (`query_card_list`, `send_card_command`) and outdoor history (`query_earth_outdoor_history`) returned nothing live.
- Unknown meaning: `relay`, `sensor_weight`, `threshold_temper`, `switch_schedule`, `get_stage_io`.
