# Coming from docker-wyze-bridge

Wyze Tunnel started as a copy of [mrlt8/docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge), which hasn't been updated since September 2024. Most things carry over unchanged:

- **Settings:** every option you set (`QUALITY`, `FILTER_NAMES`, `RECORD_ALL`, `STREAM_AUTH`, `NET_MODE`, `ROTATE_DOOR` and the rest) has the same name and meaning.
- **Stream links:** cameras keep the same names, so `rtsp://<host>:8554/front-door` and the HLS and WebRTC links still work. Frigate, Blue Iris, Home Assistant and VLC need no changes, as long as Wyze Tunnel runs on the same machine and ports.
- **Ports:** web page 5000, RTSP 8554, HLS 8888, WebRTC 8889, RTMP 1935.

## If you use Docker

Change one line in your `docker-compose.yml` and keep everything else, volumes included:

```yaml
image: ghcr.io/itskiwi/wyze-tunnel:latest   # was mrlt8/wyze-bridge
```

Then run `docker compose pull && docker compose up -d`. Your `/tokens` volume keeps you signed in. Using the Home Assistant add-on? Add this repository in the add-on store, install **Wyze Tunnel**, sign in once and stop the old add-on.

## If you want to leave Docker

The [Windows app](../README.md#windows-10-and-11-recommended), the [Linux one-liner](../README.md#linux) and the [Tunnel Box](../README.md#tunnel-box-raspberry-pi-or-mini-pc) don't need Docker. To bring your settings along:

1. Install Wyze Tunnel and open its web page.
2. Go to **Account**, then **Coming from docker-wyze-bridge?**, and paste your `docker-compose.yml` or `.env` file.
3. Copy the lines it gives you into the settings file it names, then restart Wyze Tunnel:
   - Windows: `%LOCALAPPDATA%\Wyze Tunnel\wyze-tunnel.env`, then **Stop** and **Start** in the tray menu.
   - Linux and Tunnel Box: `/etc/wyze-tunnel/wyze-tunnel.env`, then `wyze-tunnel restart`.
4. Stop the old container (`docker compose down`) so the two don't fight over the same ports.

The converter leaves out three kinds of setting, and says which ones:

- Your Wyze email, password, API key and ID. Sign in on the setup page instead, so they aren't kept in a plain file.
- Folders inside the old container, like `/media/recordings`. Recordings go to Wyze Tunnel's own folder unless you set a new one.
- Values that came from another file, like `${WYZE_EMAIL}`. Copy the real value in by hand.

## What's different

- **The web page** is new: Home, Live, Recordings, Alerts, Search and Health replace the single camera grid.
- **The stream login** for RTSP, HLS and WebRTC may change on a new machine. The **Recorders** tab under **Connect apps** shows the new links with the login filled in.
- **On Windows without WSL**, cameras connect through go2rtc (on your network) or Wyze's cloud. Camera controls work on your network, and siren, lights and motion detection go through the Wyze cloud. Only cameras away from home or on old firmware need **Full camera support (WSL)** from the tray menu.
- **New cameras:** Cam OG and others that only stream through Wyze's cloud now work. See [How Wyze Tunnel connects to each camera](../README.md#how-wyze-tunnel-connects-to-each-camera).

Something didn't carry over? [Open an issue](https://github.com/itskiwi/Wyze-Tunnel/issues) with the setting name (never your password or keys).
