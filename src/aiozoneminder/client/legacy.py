"""Client for ZoneMinder's built-in CakePHP API.

Talks to ``/api/...`` with query-parameter token authentication. This backend
works against a stock ZoneMinder install and is the fallback for people who do
not run the standalone zm-api server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from ..const import (
    API_ALARM_OFF,
    API_ALARM_ON,
    API_CONFIGS,
    API_EVENT,
    API_EVENTS,
    API_LOGIN,
    API_LOGOUT,
    API_MONITOR,
    API_MONITORS,
    API_STATE_CHANGE,
    API_STATES,
    API_VERSION,
    BACKEND_LEGACY,
    CONFIG_GO2RTC_PATH,
    CONTROL_REQUEST_PATH,
    DEFAULT_TIMEOUT,
    EVENT_VIDEO_PATH,
    LONG_TIMEOUT,
    STREAM_PATH,
    STREAM_TYPE_GO2RTC,
    STREAM_TYPE_MJPEG,
)
from ..exceptions import (
    ZMApiError,
    ZMAuthenticationError,
    ZMConnectionError,
    ZMNotSupportedError,
)
from ..models import Event, Monitor, RunState, ServerInfo
from .base import ZoneMinderClient

_LOGGER = logging.getLogger(__name__)

#: Normalized PTZ command to the legacy control verb.
_PTZ_MAP = {
    "up": "moveConUp",
    "down": "moveConDown",
    "left": "moveConLeft",
    "right": "moveConRight",
    "up-left": "moveConUpLeft",
    "up-right": "moveConUpRight",
    "down-left": "moveConDownLeft",
    "down-right": "moveConDownRight",
    "stop": "moveStop",
    "zoom-in": "zoomConTele",
    "zoom-out": "zoomConWide",
    "zoom-stop": "zoomStop",
    "focus-near": "focusConNear",
    "focus-far": "focusConFar",
    "focus-auto": "focusAuto",
    "focus-stop": "focusStop",
    "home": "presetHome",
    "preset": "presetGoto",
}

#: Pan and tilt speed, in percent of the camera's range, for continuous moves.
#: ZoneMinder only adds a speed to the command when xge or yge is set, and
#: several control modules treat a missing speed as zero, which is a stop.
_PTZ_SPEED = 50

#: ZoneMinder 1.37 renamed the single Function field. Writing both keeps this
#: backend working across 1.36 and 1.37+.
_FUNCTION_FROM_STATE = {
    ("None", "None", "None"): "None",
    ("Always", "None", "None"): "Monitor",
    ("Always", "Always", "OnMotion"): "Modect",
    ("Always", "None", "Always"): "Record",
    ("Always", "Always", "Always"): "Mocord",
    ("Always", "None", "OnMotion"): "Nodect",
}


class ZoneMinderLegacyClient(ZoneMinderClient):
    """Client for ZoneMinder's built-in API."""

    backend = BACKEND_LEGACY

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the client."""
        super().__init__(*args, **kwargs)
        self._auth_token: str | None = None
        self._go2rtc: dict[str, Any] | None = None
        self._go2rtc_checked = False
        self._go2rtc_streams: dict[int, str] = {}

    # -- Authentication ------------------------------------------------------

    async def authenticate(self) -> bool:
        """Log in and store the API token."""
        try:
            async with self._session.post(
                self._build_url(API_LOGIN),
                data={"username": self._username, "password": self._password},
                timeout=self._timeout,
                ssl=self._verify_ssl,
                allow_redirects=True,
            ) as response:
                if response.status in (401, 403):
                    raise ZMAuthenticationError("Invalid username or password")
                if response.status == 404:
                    raise ZMConnectionError(
                        f"No ZoneMinder API at {self._host}. Check the URL includes "
                        "the ZoneMinder path (for example http://host/zm) and that "
                        "OPT_USE_API is enabled."
                    )
                response.raise_for_status()
                result = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ZMConnectionError(f"Connection failed: {err}") from err

        self._auth_token = result.get("access_token") or result.get("credentials")
        self._authenticated = True
        return True

    async def close(self) -> None:
        """Log out."""
        if self._authenticated:
            try:
                async with self._session.get(
                    self._build_url(API_LOGOUT),
                    params={"token": self._auth_token} if self._auth_token else {},
                    timeout=self._timeout,
                    ssl=self._verify_ssl,
                ) as response:
                    response.raise_for_status()
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Logout failed: %s", err)
        self._authenticated = False
        self._auth_token = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Make an authenticated request, retrying once after re-authenticating."""
        if not self._authenticated:
            await self.authenticate()

        url = self._build_url(endpoint)
        base_params = kwargs.pop("params", {})

        for attempt in (1, 2):
            params = dict(base_params)
            if self._auth_token:
                params["token"] = self._auth_token
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    timeout=self._timeout,
                    ssl=self._verify_ssl,
                    **kwargs,
                ) as response:
                    if response.status in (401, 403):
                        if attempt == 2:
                            raise ZMAuthenticationError(
                                "Authentication failed after refreshing the token"
                            )
                        self._authenticated = False
                        await self.authenticate()
                        continue
                    if response.status == 404:
                        raise ZMApiError(f"Not found: {endpoint}")
                    response.raise_for_status()
                    return await response.json(content_type=None)
            except aiohttp.ClientResponseError as err:
                raise ZMApiError(
                    f"{endpoint} returned {err.status}: {err.message}"
                ) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise ZMConnectionError(f"Request to {endpoint} failed: {err}") from err

        raise ZMApiError(f"Request to {endpoint} did not complete")

    # -- Server --------------------------------------------------------------

    async def get_version(self) -> str:
        """Return the ZoneMinder version."""
        response = await self._request("GET", API_VERSION)
        return str(response.get("version") or "unknown")

    async def get_server_info(self) -> ServerInfo:
        """Return the version. The legacy API exposes no daemon or disk data."""
        return ServerInfo(version=await self.get_version())

    async def get_configs(self) -> dict[str, str]:
        """Return ZoneMinder's configuration keys and values."""
        response = await self._request("GET", API_CONFIGS)
        configs: dict[str, str] = {}
        for item in response.get("configs") or []:
            config = item.get("Config", item)
            name = config.get("Name")
            if name:
                configs[name] = config.get("Value") or ""
        return configs

    # -- Monitors ------------------------------------------------------------

    async def get_monitors(self) -> list[Monitor]:
        """Return every monitor."""
        response = await self._request("GET", API_MONITORS)
        monitors = []
        for item in response.get("monitors") or []:
            try:
                monitors.append(Monitor.from_dict(item))
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping unparsable monitor: %s", err)
        return monitors

    async def get_monitor(self, monitor_id: int) -> Monitor:
        """Return a single monitor."""
        response = await self._request("GET", API_MONITOR.format(monitor_id=monitor_id))
        return Monitor.from_dict(response.get("monitor", response))

    async def set_monitor_state(
        self,
        monitor_id: int,
        *,
        capturing: str | None = None,
        analysing: str | None = None,
        recording: str | None = None,
        decoding: str | None = None,
    ) -> None:
        """Update a monitor's state fields.

        ZoneMinder 1.37+ accepts the split fields; older releases only accept
        ``Function``. We send both, filling any unspecified field from the
        monitor's current state so the derived Function stays correct.
        """
        current = await self.get_monitor(monitor_id)
        capturing = capturing if capturing is not None else current.capturing
        analysing = analysing if analysing is not None else current.analysing
        recording = recording if recording is not None else current.recording

        data = {
            "Monitor[Capturing]": capturing,
            "Monitor[Analysing]": analysing,
            "Monitor[Recording]": recording,
        }
        if decoding is not None:
            data["Monitor[Decoding]"] = decoding
        if function := _FUNCTION_FROM_STATE.get((capturing, analysing, recording)):
            data["Monitor[Function]"] = function

        await self._request(
            "POST", API_MONITOR.format(monitor_id=monitor_id), data=data
        )

    # -- Run states ----------------------------------------------------------

    async def get_run_states(self) -> list[RunState]:
        """Return the server's saved run states."""
        response = await self._request("GET", API_STATES)
        return [
            RunState.from_dict(item.get("State", item))
            for item in (response or {}).get("states", [])
        ]

    async def set_run_state(self, state: RunState) -> None:
        """Switch run state; ZoneMinder applies it and restarts its daemons."""
        await self._request(
            "POST", API_STATE_CHANGE.format(name=quote(state.name, safe=""))
        )

    # -- Events --------------------------------------------------------------

    async def get_events(
        self, monitor_id: int | None = None, limit: int = 100
    ) -> list[Event]:
        """Return events newest first, optionally filtered to one monitor."""
        endpoint = API_EVENTS
        if monitor_id is not None:
            # The CakePHP API expresses filters as path segments.
            endpoint = f"api/events/index/MonitorId:{monitor_id}.json"

        response = await self._request(
            "GET",
            endpoint,
            params={"sort": "StartTime", "direction": "desc", "limit": limit},
        )
        events = []
        for item in response.get("events") or []:
            try:
                events.append(Event.from_dict(item, self._timezone))
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.debug("Skipping unparsable event: %s", err)
        events.sort(key=lambda event: event.start_time, reverse=True)
        return events[:limit]

    async def get_event(self, event_id: int) -> Event:
        """Return a single event."""
        response = await self._request("GET", API_EVENT.format(event_id=event_id))
        return Event.from_dict(response.get("event", response), self._timezone)

    # -- Alarm and PTZ -------------------------------------------------------

    async def trigger_alarm(self, monitor_id: int, cause: str) -> None:
        """Force a monitor into alarm state."""
        await self._request(
            "GET", API_ALARM_ON.format(monitor_id=monitor_id), params={"cause": cause}
        )

    async def cancel_alarm(self, monitor_id: int) -> None:
        """Clear a forced alarm."""
        await self._request("GET", API_ALARM_OFF.format(monitor_id=monitor_id))

    async def ptz(self, monitor_id: int, command: str, **kwargs: Any) -> None:
        """Issue a PTZ command through ZoneMinder's control request."""
        control = _PTZ_MAP.get(command)
        if control is None:
            raise ZMNotSupportedError(f"Unsupported PTZ command: {command}")

        if command == "preset":
            preset = kwargs.get("preset")
            if preset is None:
                raise ZMApiError("The 'preset' command requires a preset number")
            control = f"{control}{int(preset)}"

        params: dict[str, Any] = {
            "view": "request",
            "request": "control",
            "id": monitor_id,
            "control": control,
        }
        if control.startswith("move") and command != "stop":
            params["xge"] = params["yge"] = _PTZ_SPEED

        try:
            response = await self._request("POST", CONTROL_REQUEST_PATH, params=params)
        except ValueError as err:
            # Not JSON: a login page or an error page instead of the reply.
            raise ZMApiError(f"PTZ {command} got no reply from ZoneMinder") from err
        # The control request answers 200 with result "Error" when it refuses.
        if not isinstance(response, dict) or response.get("result") != "Ok":
            message = response.get("message") if isinstance(response, dict) else None
            raise ZMApiError(f"PTZ {command} failed: {message or response!r}")

    # -- Streaming -----------------------------------------------------------

    def get_stream_url(self, monitor_id: int, mode: str = "jpeg") -> str:
        """Build an MJPEG or single-frame URL for ZoneMinder's streaming CGI."""
        params = {
            "mode": mode,
            "monitor": monitor_id,
            "maxfps": 30,
            "buffer": 1000,
        }
        if self._auth_token:
            params["token"] = self._auth_token
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"{self._build_url(STREAM_PATH)}?{query}"

    def get_event_clip_url(self, event_id: int) -> str:
        """Build a URL for an event's MP4 clip."""
        url = f"{self._host}/{EVENT_VIDEO_PATH.format(event_id=event_id)}"
        if self._auth_token:
            url += f"?token={self._auth_token}"
        return url

    def get_stream_type(self, monitor_id: int) -> str:
        """Report the stream type actually being served.

        A go2rtc instance with its RTSP server switched off cannot be pulled
        from, so those monitors are MJPEG however many streams go2rtc lists.
        """
        if self._go2rtc_rtsp_url(monitor_id) is not None:
            return STREAM_TYPE_GO2RTC
        return STREAM_TYPE_MJPEG

    async def get_stream_source(self, monitor_id: int) -> str | None:
        """Return a go2rtc RTSP URL when one is usable, else the MJPEG URL."""
        await self._async_discover_go2rtc()
        if rtsp := self._go2rtc_rtsp_url(monitor_id):
            return rtsp
        return self.get_stream_url(monitor_id)

    async def async_get_snapshot(self, monitor_id: int) -> bytes | None:
        """Return one JPEG still frame."""
        url = self.get_stream_url(monitor_id, mode="single")
        try:
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=LONG_TIMEOUT),
                ssl=self._verify_ssl,
            ) as response:
                response.raise_for_status()
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Snapshot for monitor %d failed: %s", monitor_id, err)
            return None

    @asynccontextmanager
    async def async_stream_media(
        self, path: str, headers: dict[str, str] | None = None
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Yield an upstream response for a media path.

        The legacy API authenticates with a query token, so its media URLs work
        directly and this only exists to satisfy the common contract.
        """
        if not self._authenticated:
            await self.authenticate()
        url = self._build_url(path)
        params = {"token": self._auth_token} if self._auth_token else {}
        async with self._session.get(
            url,
            params=params,
            headers=headers or {},
            timeout=aiohttp.ClientTimeout(
                total=None, sock_connect=DEFAULT_TIMEOUT, sock_read=LONG_TIMEOUT
            ),
            ssl=self._verify_ssl,
            allow_redirects=False,
        ) as response:
            yield response

    # -- go2rtc --------------------------------------------------------------

    async def _async_discover_go2rtc(self) -> dict[str, Any] | None:
        """Find ZoneMinder's go2rtc instance and map monitors to its streams.

        ZoneMinder names go2rtc streams after the monitor, but not always
        exactly: a monitor can appear as ``3``, ``3_0``, ``3_h264`` or
        ``3_ZoneMinderPrimary`` depending on how it was set up. Guessing the
        name is what made this silently fall back to MJPEG before, so the
        stream list is read and matched instead.
        """
        if self._go2rtc_checked:
            return self._go2rtc
        self._go2rtc_checked = True

        try:
            configs = await self.get_configs()
        except (ZMApiError, ZMConnectionError) as err:
            _LOGGER.debug("Could not read ZoneMinder configuration: %s", err)
            return None

        api_url = configs.get(CONFIG_GO2RTC_PATH)
        if not api_url:
            _LOGGER.debug("go2rtc is not configured (%s is unset)", CONFIG_GO2RTC_PATH)
            return None

        api_url = api_url.rstrip("/")
        base_url = api_url[: -len("/api")] if api_url.endswith("/api") else api_url

        try:
            async with self._session.get(
                f"{api_url}/streams", timeout=self._timeout
            ) as response:
                response.raise_for_status()
                streams = await response.json(content_type=None)
            async with self._session.get(api_url, timeout=self._timeout) as response:
                response.raise_for_status()
                info = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("go2rtc at %s did not respond: %s", api_url, err)
            return None

        rtsp_port = _parse_rtsp_port(info)
        if rtsp_port is None:
            _LOGGER.info(
                "go2rtc at %s has its RTSP server disabled, so it can't be "
                "pulled from. Falling back to MJPEG",
                base_url,
            )

        self._go2rtc = {
            "api_url": api_url,
            "base_url": base_url,
            "rtsp_port": rtsp_port,
            "host": _parse_host(base_url),
        }
        self._go2rtc_streams = _match_streams(streams)
        _LOGGER.info(
            "go2rtc at %s: %d monitor streams matched",
            base_url,
            len(self._go2rtc_streams),
        )
        return self._go2rtc

    def _go2rtc_rtsp_url(self, monitor_id: int) -> str | None:
        """Return the go2rtc RTSP URL for a monitor, if one is usable."""
        if not self._go2rtc:
            return None
        port = self._go2rtc.get("rtsp_port")
        host = self._go2rtc.get("host")
        stream = self._go2rtc_streams.get(monitor_id)
        if not port or not host or not stream:
            return None
        return f"rtsp://{host}:{port}/{stream}"

    async def check_go2rtc(self) -> dict[str, Any] | None:
        """Return the discovered go2rtc configuration, if any."""
        return await self._async_discover_go2rtc()


def _parse_host(url: str) -> str | None:
    """Extract the hostname from a URL."""
    match = re.match(r"https?://([^:/]+)", url)
    return match.group(1) if match else None


def _parse_rtsp_port(info: dict[str, Any]) -> int | None:
    """Return go2rtc's RTSP port, or None when its RTSP server is off.

    go2rtc reports ``{"rtsp": {"listen": ":8554"}}``. An empty listen address
    means no RTSP server is running, which is the default in some ZoneMinder
    packages.
    """
    listen = (info.get("rtsp") or {}).get("listen") or ""
    match = re.search(r":(\d+)$", listen)
    return int(match.group(1)) if match else None


def _match_streams(streams: Any) -> dict[int, str]:
    """Map monitor ids to go2rtc stream names.

    Prefers an exact numeric match, then the primary ``<id>_0`` sub-stream,
    then any other stream whose name starts with the monitor id.
    """
    if not isinstance(streams, dict):
        return {}

    candidates: dict[int, list[str]] = {}
    for name in streams:
        match = re.match(r"^(\d+)(?:_.*)?$", str(name))
        if match:
            candidates.setdefault(int(match.group(1)), []).append(str(name))

    matched: dict[int, str] = {}
    for monitor_id, names in candidates.items():
        exact = str(monitor_id)
        if exact in names:
            matched[monitor_id] = exact
        elif f"{monitor_id}_0" in names:
            matched[monitor_id] = f"{monitor_id}_0"
        else:
            matched[monitor_id] = sorted(names)[0]
    return matched
