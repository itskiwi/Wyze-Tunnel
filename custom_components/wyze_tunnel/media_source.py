"""Recordings in Home Assistant's Media browser: Wyze Tunnel, then a camera, then its clips."""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote, unquote

from aiohttp import ClientError, ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import BrowseMediaSource, MediaSource, MediaSourceItem, PlayMedia, Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util

from . import DOMAIN

MAX_CLIPS = 200  # newest first; the Media browser gets slow with thousands


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return WyzeTunnelMediaSource(hass)


def _coordinator(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise Unresolvable("That Wyze Tunnel isn't set up")
    return entry.runtime_data


class WyzeTunnelMediaSource(MediaSource):
    name = "Wyze Tunnel"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        entry_id, cam, start, duration = (item.identifier or "").split("/")
        _coordinator(self.hass, entry_id)
        path = f"/api/wyze_tunnel/clip/{entry_id}/{cam}/{start}/{duration}"
        return PlayMedia(async_sign_path(self.hass, path, timedelta(hours=1)), "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = item.identifier.split("/") if item.identifier else []
        if not parts:
            children = [
                self._folder(f"{entry.entry_id}/{cam_id}", cam["name"])
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
                for cam_id, cam in entry.runtime_data.data.items()
            ]
            return self._folder("", "Wyze Tunnel", children)
        entry_id, cam = parts[0], parts[1]
        coordinator = _coordinator(self.hass, entry_id)
        try:
            clips = await coordinator.api.recordings(cam)
        except (ClientError, TimeoutError, ValueError) as ex:
            raise Unresolvable(f"Can't reach Wyze Tunnel: {ex}") from ex
        children = [self._clip(entry_id, cam, clip) for clip in reversed(clips[-MAX_CLIPS:])]
        name = (coordinator.data.get(cam) or {}).get("name", cam)
        return self._folder(item.identifier, name, children)

    def _folder(self, identifier: str, title: str, children: list | None = None) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN, identifier=identifier, media_class=MediaClass.DIRECTORY, media_content_type="",
            title=title, can_play=False, can_expand=True, children=children, children_media_class=MediaClass.VIDEO,
        )

    def _clip(self, entry_id: str, cam: str, clip: dict) -> BrowseMediaSource:
        start = datetime.fromisoformat(clip["start"].replace("Z", "+00:00"))
        mins = round(clip["duration"] / 60)
        title = f"{dt_util.as_local(start):%a %b %-d, %-I:%M %p} · {mins} min" if mins else f"{dt_util.as_local(start):%a %b %-d, %-I:%M:%S %p}"
        return BrowseMediaSource(
            domain=DOMAIN, identifier=f"{entry_id}/{cam}/{quote(clip['start'], safe='')}/{clip['duration']}",
            media_class=MediaClass.VIDEO, media_content_type=MediaType.VIDEO, title=title, can_play=True, can_expand=False,
        )


class ClipView(HomeAssistantView):
    """Plays a clip through Home Assistant, which holds the key; the signed link from the Media browser gets in."""

    url = "/api/wyze_tunnel/clip/{entry_id}/{cam}/{start}/{duration}"
    name = "api:wyze_tunnel:clip"

    async def get(self, request: web.Request, entry_id: str, cam: str, start: str, duration: str) -> web.StreamResponse:
        try:
            api = _coordinator(request.app[KEY_HASS], entry_id).api
        except Unresolvable:
            raise web.HTTPNotFound
        params = {"start": unquote(start), "duration": duration}
        try:
            async with api.session.get(
                api.play_url(cam), params=params, headers=api.headers, timeout=ClientTimeout(total=None, sock_read=60)
            ) as upstream:
                if upstream.status != 200:
                    raise web.HTTPBadGateway(text=f"Wyze Tunnel answered {upstream.status}")
                resp = web.StreamResponse(headers={"Content-Type": "video/mp4"})
                await resp.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                return resp
        except (ClientError, TimeoutError) as ex:
            raise web.HTTPBadGateway(text="Can't reach Wyze Tunnel") from ex
