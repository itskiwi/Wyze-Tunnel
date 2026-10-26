# Two-way talk: notes

Push-to-talk on the Live page sends the browser's microphone to a camera's speaker. This page records how Wyze cameras take talk audio, which paths Wyze Tunnel uses, and what is still untested.

## What ships

A microphone button sits in each camera's tile bar (`app/templates/live.html`). Hold it to talk; let go to stop. It shows for cameras with `camera.webrtc` (models not in `NO_WEBRTC`), and not for watch-only guests.

While the button is held:

1. The browser asks for the mic (`app/static/webrtc.js`, `talk()`).
2. It fetches `signaling/<cam>?kvs&talk`. The server returns a Wyze Kinesis (KVS) signal even when the tunnel is already streaming that camera (`app/frontend.py`, `webrtc_signaling`). Watch-only guests get a 403; family logins are allowed, since they have full access.
3. `new Receiver(signal, mic)` opens a second, talk-only KVS session. It offers video `recvonly` plus an audio transceiver that carries the mic track, `sendrecv`. The video it receives is ignored, because the tile already plays the camera.
4. On release, `Receiver.stop()` closes the peer connection and the WebSocket and stops the mic tracks. A stopped session never reconnects.

Button states: amber means connecting, red means live (ICE connected), and a red ring means the talk failed (the reason is in the tooltip). Browsers only give the mic to `https://` or `http://localhost` pages; on a plain-http LAN address the button is disabled and says so.

## Why a second KVS session, not go2rtc's backchannel

go2rtc v1.9.14 supports two-way audio for its native Wyze source, but that doesn't help the Live page:

- **The KVS (`webrtc:`) source in go2rtc is receive-only.** `internal/webrtc/kinesis.go:117-119` builds both medias as `DirectionRecvonly`. A camera that the tunnel reaches through Wyze's cloud gets no backchannel from go2rtc.
- **The LAN (`wyze://`) source does have a backchannel, but in the camera's own codec.** `pkg/wyze/producer.go:255-264` adds a `sendonly` audio media whose codec clones what the camera sends: AAC on the v4, 16-bit PCM on the v3 and Doorbell v2, PCMU on the v2 (go2rtc's `internal/wyze/README.md` compatibility table). `pkg/wyze/backchannel.go:12-13` (`AddTrack`) turns on the intercom and writes the consumer's packets as they are. A browser can only send Opus, PCMU or PCMA, so only the v2 would match without a transcode.
- **The browser can't reach go2rtc's WebRTC.** The bridge binds go2rtc's API to `127.0.0.1:1984` and turns its WebRTC listener off (`app/wyzebridge/go2rtc.py`, `Go2rtcServer.config`). The live grid plays through MediaMTX WHEP, which carries nothing back to the camera.
- **LAN-only.** The `wyze://` source needs the camera on the bridge's own network. These cameras are at other homes.

The Wyze app talks to KVS cameras exactly the way this change does (paths are in the jadx output of the Wyze APK, v3.24):

- `com/wyze/camerasdk/core/webrtc/internal/WebRtcClient.java:962` creates a `KvsAudioTrack` from the mic.
- `:839-852` adds that track to the peer connection when the session starts.
- `:1258` (`T`), `:1537` (`r0`) and `:1555` (`u0`) start and stop talking by toggling the track's `enabled` flag and muting or unmuting the mic. No extra command is sent.
- `:995` also opens a `wyzeWebRTCDataChannel`, but the talk toggle doesn't use it.

## TUTK talk path (documented, not built)

TUTK cameras that aren't on WebRTC need this path, such as the Doorbell v2 (`HL_DB2` is in `NO_WEBRTC`). It is reconstructed from the APK:

| Step | APK reference | Detail |
|---|---|---|
| Enable return audio | `com/HLApi/CameraAPI/connection/ConnectControl.java:2598` → `mediaEnableControl(3, true)` (`:2076`) → `CamCommand.K10010_channelControl` (`protocol/CamCommand.java:185`) | K10010 with media type 3, "Return Audio". This is `K10010ControlChannel(media_type=3, enabled=True)` in `app/wyzecam/tutk/tutk_protocol.py`. |
| Open the speak channel | `TUTKAVClient.java:1147` `startSpeak` | The phone acts as the AV **server** on IOTC channel 1. With DTLS it calls `avServStartEx`, with `iotc_channel_id=1`, `server_type=16`, `security_mode=1`, `timeout_sec=15`, `resend=0`, and a password callback that answers user `admin` with the camera's password (`:1155-1173`). Without DTLS it calls `avServStart2(sid, null, null, 10, 16, 1)` (`:1176`). Outdoor (base-station) cameras use the parent's MAC and DTLS flag (`ConnectControl.java:2594-2597`). |
| Send audio | `TUTKAVClient.java:1050` `sendSpeakData` | `avSendAudioData(speakIndex, data, len, frameHeader, headerLen)`. The header is `FrameHeadInfo` (`FrameHeadInfo.java:90-92`): 40 bytes for codec 144, otherwise 24. It holds the codec id, flags, a frame number that counts up, and a timestamp in seconds. |
| Codec | `media/SpeakDataProcess.java:170-172`, `ConnectControl.java:2569-2581` | 144 = AAC, the default (`useAAC`); 140 = PCM; 146 = Opus, used only when the camera itself reports audio codec 146. The sample rate is 8 kHz, or 16 kHz on the models in `AUDIO_16k`. |
| Stop | `ConnectControl.java:2706-2708` → `TUTKAVModel.stopSpeak` | Stop the speak server, then `K10010(3, false)`. |

Building this would need:

- `avServStartEx` and `avSendAudioData` bindings plus a password callback in `app/wyzecam/tutk/tutk.py`.
- An AAC or PCM encoder in each camera's stream process.
- A way to get mic audio from the browser into that process.

None of it can be checked without playing audio on a camera, so it is left out.

## Which cameras get talk, and how

| Camera | Model | Talk path in this change |
|---|---|---|
| Wyze Cam v2 | WYZEC1-JZ | KVS mic track (untested) |
| Wyze Cam v3 | WYZE_CAKP2JFUS | KVS mic track (untested) |
| Wyze Cam v4 | HL_CAM4 | KVS mic track (untested) |
| Wyze Cam Pan v3 | HL_PAN3 | KVS mic track (untested) |
| Wyze Cam Floodlight v2 | HL_CFL2 | KVS mic track (untested) |
| Wyze Battery Cam Pro | HL_BC | KVS mic track (untested) |
| Wyze Cam OG | GW_GC1 | KVS mic track (untested; Gwell signaling may differ) |
| Wyze Video Doorbell v2 | HL_DB2 | None: in `NO_WEBRTC`, so no button. It would need the TUTK path above, or go2rtc's LAN backchannel on the same network. |

The button only appears when the camera's model is not in `NO_WEBRTC`. If Wyze refuses the KVS signal (for example a 404 for a camera without WebRTC), the button shows the error.

## Not verified

- **No audio was played through any camera.** Every camera on the account is at another home.
- Whether each model plays incoming KVS audio. The Wyze app does this, but browser Opus may differ from what the app's WebRTC build negotiates.
- Whether a camera accepts a second KVS viewer while the tunnel holds one. The Cam OG takes two at once; when go2rtc's `webrtc` source is in use, the talk session is the second.
- The Gwell (OG) KVS signal shape with this player.

## Live test for the owner

Do this at a camera where someone at the other end expects it, at low volume:

1. Open Wyze Tunnel at `http://localhost:5050` (the mic needs localhost or https).
2. On the Live page, find a Wyze Cam v3 tile and hold the microphone button. Allow the mic when the browser asks.
3. The button turns amber, then red within about 5 seconds. Say a few words, then let go. The button returns to grey.
4. Ask the person at the camera whether they heard you, and how far behind the audio was.
5. If it stayed amber or showed a red ring, open the browser console (filter `talk:`) and share the error. Also check `journalctl -u wyze-tunnel` for "Fetching signaling data".
6. Repeat on one more model (the v4 or the Pan v3) to cover the AAC models.
