# SD card playback: how the Wyze app does it

Notes from the decompiled Wyze Android app (v3.24, jadx). Paths are under `sources/com/`.
They cover the TUTK cameras Cam v3 (`WYZE_CAKP2JFUS`), v4 (`HL_CAM4`), Pan v3 (`HL_PAN3`),
Floodlight v2 (`HL_CFL2`), v2 (`WYZEC1-JZ`) and Doorbell v2 (`HL_DB2`).
Everything is little endian and goes over the usual `HL` ioctl channel (the bridge's `encode()`); answers come back as code + 1.

## Which command each camera uses

`ConnectControl.func_getRecordInfoInMinutes` (`HLApi/CameraAPI/connection/ConnectControl.java:1168`) sends
K10350 when `CameraSupportFunc.isSurpport(model, protocol, INDEX_GET_RECORD_INFO)` is true, and the older K10232 otherwise.
The minimum protocol is 8 for all six cameras: `HLApi/Obj/CameraSupportFunc.java:97` (v2's `proTable_WyzeC1_JZ`) and
`:102` (`proTable_Wyze_CAMV32`, used for v3, Pan v3 and Floodlight v2 at `:337-343` and for v4 and Doorbell v2 at `:324-327`).
The outdoor cams (`WVOD1`, `WVOD2`, `WVODB1`) are `Integer.MAX_VALUE` there, so they always use K10232
(they also have their own clip list, K10438, at `ConnectControl.java:1164`).

## Listing a day: K10350 → 10351

Request (`HLApi/CameraAPI/protocol/CamCommand.java:610`), 8 bytes:

| byte | value |
|---|---|
| 0 | 1 |
| 1 | 1 = by minute (2 = by hour, `CamCommand.java:601`) |
| 2-5 | u32 window start, epoch seconds |
| 6-7 | u16 720 (minutes) |

`ByteOperator.byteArrayCopy` copies an *inclusive* end index (`HLApi/utils/ByteOperator.java:74`), so `..., 0, 3)` is 4 bytes.

A day is two windows: `func_getRecordInfoInMinutes(start, isAm)` adds 0 or 43200 (`ConnectControl.java:1169`).
The v3-plugin page (v3, v4, Pan, Floodlight, Doorbell) passes **local midnight**,
`CommonMethod.getZeroClockTimestamp` (`HLApi/utils/CommonMethod.java:1409`), for example at
`hualai/cam_v3/camera/activity/FragmentPlaybackPage.java:873` and `:1316-1320`.

Response 10351 (`HLApi/CameraAPI/protocol/CommandTreatment.java:1298`, needs ≥ 101 bytes), parsed in
`HLApi/Obj/ReplayInfoPerDay.java` and `ReplayInfoPerDayForZero.java`:

| byte | value |
|---|---|
| 0 | 1 |
| 1 | 1 (the mode echoed) |
| 2 | 1 if there is an SD card ("No SD Card" otherwise) |
| 3-6 | u32 window start, echoed |
| 7-8 | u16 length (720) |
| 9-10 | u16 n, the bitmap's length in bytes (90) |
| 11.. | bitmap, one bit per minute from the window start, LSB first |

`ConnectControlHandler.java:537-547` picks `ReplayInfoPerDayForZero` (local-midnight windows) when
`CommonMethod.isSupportZeroClockModel` (`CommonMethod.java:1564`: v3, WZ and Pan cams) and `ReplayInfoPerDay` otherwise.
The second one snaps the start to a UTC day (`ReplayInfoPerDay.java`, `startTimeInSec = start / 86400 * 86400`).
The bridge places bits from the echoed start, which matches both when the camera echoes the window it was asked for.

Other related commands, not used by the bridge yet:

- K10352 → 10353, the first and last recorded time: `[1:5]` and `[5:9]` as u32 (`CamCommand.java:619`, `CommandTreatment.java:1314`, `hualai/plugin/camera/activity/MainActivityHandler.java:825-846`).
- K10350 mode 2, by hour (`ConnectControl.java:2886`; bitmap parse at `FragmentPlaybackPage.java:1472-1515`).
- K10800, days that have recordings, JSON (`CamCommand.java:1362`, `ConnectControl.java:1201`).
- Legacy K10232 → 10233 (u32 start, then 720 one-byte minute flags, `CommandTreatment.java:1142`) and K10230 → 10231 (`CamCommand.java:494-498`, `CommandTreatment.java:1136`).

## Playing: K10150 → 10151

`ConnectControl.func_replay(start, type, ts)` (`ConnectControl.java:1307`) stores `playbackToken = (int) ts` and sends
`CamCommand.K10150_replay` (`CamCommand.java:365`), 6 bytes:

| byte | value |
|---|---|
| 0 | 1 = start, 2 = stop |
| 1 | video type (1) |
| 2-5 | u32 epoch seconds to play from |

The answer 10151 is one byte, 1 = OK (`CommandTreatment.java:974`, `MainActivityHandler.java:590`).
**Seeking is another start with a new ts** (dragging the timeline: `FragmentPlaybackPage.java:312-320`, clamped to now − 70 s);
stop is `func_replay(false, ...)` (`FragmentPlaybackPage.java:373`).

## Frames

Playback frames arrive on the **same AV channel as live**, through the normal `avRecvFrameData2`; there is no separate channel.
The frame header (`HLApi/CameraAPI/connection/FrameHeadInfo.java:35-68`) matches the bridge's `FrameInfoStruct`:
codec at 0, flags at 2, fps at 5, µs at 8, seconds at 12, length at 16, frame number at 20, MAC at 24-35, and
**`frame_token` at 36** (`n_play_token` in the bridge). The token is the ts of the K10150 that produced the frame.
In replay mode the app keeps frames whose token equals `playbackToken` or is 0 and drops the rest
(`HLApi/CameraAPI/connection/ConnectControlVideoDecoderThread.java:58` and `:75`), so frames left over from before a seek are thrown away.
The app paces playback by sleeping `1000 / fps` per frame (same file, just after `:58`).

## What the bridge does with this

- `K10350GetRecordInfo` and `K10150Replay` in `app/wyzecam/tutk/tutk_protocol.py`.
- `GET /sd/<cam>?start=<local midnight>` sends two `sd_minutes` (K10350) commands through the live session's control queue and merges the minute runs.
- `POST /sd/<cam>/play {"ts": …}` starts `app/wyzebridge/sd_playback.py`: its own TUTK session (with a different phone_id, so live isn't dropped),
  K10150, frames filtered by token and paced by their timestamps, piped into ffmpeg and published at MediaMTX path `<cam>-sd`.
  Another play while it runs is a seek. `POST /sd/<cam>/stop`, 30 s without frames or 30 minutes end it.
- Recordings page: a "Camera SD card" section with a camera, a day, the list of recordings and a player.

## Not checked on a camera yet

On 2026-10-25 a read-only probe (K10350 ×2 and K10352 only, on its own phone_id) could not connect:
`IOTC_ER_TIMEOUT` on a v3 and a v4. The bridge's own TUTK sessions had been failing too (`IOTC_ER_DEVICE_OFFLINE`, `IOTC_ER_TIMEOUT`),
so this was a connectivity problem, not the new commands. Still open:

- the 10351 layout and the echoed start on a real card, mainly the v2's UTC-day snapping;
- whether a second session is allowed while live runs (the camera's session limit);
- whether replay frames really carry the token, and how fast the card sends them.
