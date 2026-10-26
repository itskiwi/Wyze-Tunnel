# Device groups: what the app proves

The Wyze app groups devices (e.g. camera groups, a room of plugs) and shows the group name on each device. The group list rides
along in the same `get_object_list` response the bridge already fetches, so surfacing it costs no extra request and no signing.

- `v2/home_page/get_object_list` returns `device_group_list` next to `device_list`
  (`com/wyze/platformkit/model/DeviceModel.java:17-18`).
- Each group is `DeviceGroupData` (`DeviceModel.java:653-685`): `group_id` (int), `group_name`, `group_type_id` (int),
  `device_list` (the members, each a `DeviceData` with `mac`/`device_mac`), plus `logo_url`/`group_type_logo_url` (images, dropped).
- The app reads it the same way: `device_group_list = deviceModel.getDevice_group_list()` then iterates the members
  (the sun plug module, `DeviceMainPage` group handling).

Implemented read-only:
- `devices._groups(data)` maps each entry to `{id, name, type, macs}`; `devices.list_devices` tags each device with `d["group"]`
  (its group name) and caches the group list; `devices.groups(auth)` returns it.
- `GET /api/devices/groups` returns `{"groups": [...]}`; the devices page shows `Group: <name>` in each device's facts line.

## Not done (writes)

Creating, renaming, deleting or re-membering groups is a write and was left out. The app does it through
`WpkHomeGroupManager.updateGroup` (a `WpkSignature2Service` POST to `URL_DEVICE_GROUP` with `home_id`, `group_name`, `group_id`,
`device_list`) and the auto-group endpoints (`URL_AUTO_GROUP_SET/RUN/DELETE`, ids 1112/1116/1115). Add only if asked.
