"""Tests for the API v2 (built-in CakePHP API) client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import re
from typing import Any

import aiohttp
from aioresponses import CallbackResult, aioresponses
import pytest

from aiozoneminder import (
    ZMApiError,
    ZMAuthenticationError,
    ZMConnectionError,
    ZMNotSupportedError,
    ZoneMinderLegacyClient,
)
from aiozoneminder.const import STREAM_TYPE_GO2RTC, STREAM_TYPE_MJPEG

ZM = "http://zm.local/zm"
GO2RTC = "http://192.168.0.45:1984/api"

# The stream names a real ZoneMinder go2rtc instance advertises. They are not
# plain monitor ids, which is what the old guess-the-name code assumed.
GO2RTC_STREAMS = {
    "1_0": {},
    "1_1": {},
    "2_0": {},
    "3": {},
    "3_ZoneMinderPrimary": {},
    "3_h264": {},
    "4_0": {},
}

CONFIGS_RESPONSE = {
    "configs": [
        {"Config": {"Name": "ZM_GO2RTC_PATH", "Value": GO2RTC}},
        {"Config": {"Name": "ZM_MQTT_TOPIC_PREFIX", "Value": "ZoneMinder"}},
    ]
}


@pytest.fixture
def client(session: aiohttp.ClientSession) -> ZoneMinderLegacyClient:
    """Return an API v2 client pointed at a stand-in ZoneMinder."""
    return ZoneMinderLegacyClient(ZM, "admin", "hunter2", session)


def _login(mocked: aioresponses) -> None:
    """Register a successful login."""
    mocked.post(
        f"{ZM}/api/host/login.json",
        payload={"access_token": "token"},
        repeat=True,
    )


async def test_authenticate(client: ZoneMinderLegacyClient) -> None:
    """A successful login stores the token."""
    with aioresponses() as mocked:
        _login(mocked)
        assert await client.authenticate() is True
    assert client.authenticated is True


async def test_bad_credentials(client: ZoneMinderLegacyClient) -> None:
    """A rejected login raises ZMAuthenticationError."""
    with aioresponses() as mocked:
        mocked.post(f"{ZM}/api/host/login.json", status=401)
        with pytest.raises(ZMAuthenticationError):
            await client.authenticate()


async def test_missing_api_explains_itself(client: ZoneMinderLegacyClient) -> None:
    """A 404 on login usually means the URL is missing the /zm path."""
    with aioresponses() as mocked:
        mocked.post(f"{ZM}/api/host/login.json", status=404)
        with pytest.raises(ZMConnectionError, match="OPT_USE_API"):
            await client.authenticate()


async def test_go2rtc_config_is_read_from_the_right_path(
    client: ZoneMinderLegacyClient,
) -> None:
    """The config endpoint sits at api/configs.json, not zm/api/configs.json.

    The old path produced http://host/zm/zm/api/configs.json, whose 404 was
    swallowed, so go2rtc was never detected and every camera silently fell
    back to MJPEG.
    """
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(f"{ZM}/api/configs.json?token=token", payload=CONFIGS_RESPONSE)
        mocked.get(f"{GO2RTC}/streams", payload=GO2RTC_STREAMS)
        mocked.get(GO2RTC, payload={"rtsp": {"listen": ":8554"}})

        config = await client.check_go2rtc()

    assert config is not None
    assert config["base_url"] == "http://192.168.0.45:1984"
    assert config["rtsp_port"] == 8554


async def test_go2rtc_stream_names_are_discovered(
    client: ZoneMinderLegacyClient,
) -> None:
    """Stream names are matched against the real list, not guessed."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(f"{ZM}/api/configs.json?token=token", payload=CONFIGS_RESPONSE)
        mocked.get(f"{GO2RTC}/streams", payload=GO2RTC_STREAMS)
        mocked.get(GO2RTC, payload={"rtsp": {"listen": ":8554"}})

        # Monitor 3 is published under its bare id, the others under _0.
        assert await client.get_stream_source(3) == "rtsp://192.168.0.45:8554/3"
        assert await client.get_stream_source(1) == "rtsp://192.168.0.45:8554/1_0"
        assert client.get_stream_type(1) == STREAM_TYPE_GO2RTC


async def test_falls_back_to_mjpeg_when_rtsp_is_off(
    client: ZoneMinderLegacyClient,
) -> None:
    """go2rtc ships with its RTSP server disabled in some packages.

    Handing a player an rtsp:// URL nothing is listening on produces a
    camera that never loads, so an unusable RTSP port means MJPEG instead.
    """
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(f"{ZM}/api/configs.json?token=token", payload=CONFIGS_RESPONSE)
        mocked.get(f"{GO2RTC}/streams", payload=GO2RTC_STREAMS)
        # An empty listen address means no RTSP server is running.
        mocked.get(GO2RTC, payload={"rtsp": {"listen": ""}})

        source = await client.get_stream_source(1)

    assert source.startswith(f"{ZM}/cgi-bin/nph-zms")
    assert client.get_stream_type(1) == STREAM_TYPE_MJPEG


async def test_no_go2rtc_configured(client: ZoneMinderLegacyClient) -> None:
    """Without ZM_GO2RTC_PATH there is nothing to discover."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(f"{ZM}/api/configs.json?token=token", payload={"configs": []})
        assert await client.check_go2rtc() is None
        assert await client.get_stream_source(1) is not None


async def test_set_monitor_state_writes_both_shapes(
    client: ZoneMinderLegacyClient,
) -> None:
    """1.36 wants Function, 1.37+ wants the split fields, so send both."""
    posted: dict[str, str] = {}

    def _capture(url: Any, **kwargs: Any) -> CallbackResult:
        posted.update(kwargs.get("data") or {})
        return CallbackResult(payload={})

    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            f"{ZM}/api/monitors/1.json?token=token",
            payload={
                "monitor": {
                    "Monitor": {
                        "Id": "1",
                        "Name": "Driveway",
                        "Function": "Monitor",
                        "Type": "Ffmpeg",
                        "Width": "1920",
                        "Height": "1080",
                    }
                }
            },
        )
        mocked.post(f"{ZM}/api/monitors/1.json?token=token", callback=_capture)

        await client.set_monitor_state(1, analysing="Always", recording="Always")

    assert posted["Monitor[Analysing]"] == "Always"
    assert posted["Monitor[Recording]"] == "Always"
    # Capturing was left alone, so it keeps the monitor's current value.
    assert posted["Monitor[Capturing]"] == "Always"
    assert posted["Monitor[Function]"] == "Mocord"


async def test_reauthenticates_once_on_401(client: ZoneMinderLegacyClient) -> None:
    """An expired token is refreshed and the request retried."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(f"{ZM}/api/host/getVersion.json?token=token", status=401)
        mocked.get(
            f"{ZM}/api/host/getVersion.json?token=token",
            payload={"version": "1.36.33"},
        )
        assert await client.get_version() == "1.36.33"


def _event(event_id: int, start: str) -> dict[str, Any]:
    """Build an API v2 event."""
    return {
        "Event": {
            "Id": str(event_id),
            "MonitorId": "1",
            "Name": "Event",
            "Cause": "Motion",
            "StartDateTime": start,
            "EndDateTime": None,
            "Length": "0",
            "Frames": "0",
            "AlarmFrames": "3",
        }
    }


async def test_event_times_use_the_given_timezone(
    session: aiohttp.ClientSession,
) -> None:
    """API v2 times have no zone; the client's timezone is applied."""
    sydney = timezone(timedelta(hours=11))
    client = ZoneMinderLegacyClient(ZM, "admin", "hunter2", session, timezone=sydney)
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            f"{ZM}/api/events.json?direction=desc&limit=100&sort=StartTime&token=token",
            payload={
                "events": [
                    _event(1, "2026-10-24 09:00:00"),
                    _event(2, "2026-10-24 10:00:00"),
                ]
            },
        )
        events = await client.get_events()

    assert [e.id for e in events] == [2, 1]  # Newest first.
    assert events[0].start_time == datetime(2026, 10, 23, 23, 0, tzinfo=UTC)


async def test_run_states_switch_by_name(client: ZoneMinderLegacyClient) -> None:
    """API v2 applies a run state itself, so switching is one call."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            f"{ZM}/api/states.json?token=token",
            payload={
                "states": [
                    {
                        "State": {
                            "Id": "1",
                            "Name": "default",
                            "Definition": "",
                            "IsActive": "1",
                        }
                    },
                    {
                        "State": {
                            "Id": "2",
                            "Name": "Night time",
                            "Definition": "1:Modect:1",
                            "IsActive": "0",
                        }
                    },
                ]
            },
        )
        mocked.post(f"{ZM}/api/states/change/Night%20time.json?token=token", payload={})

        states = await client.get_run_states()
        await client.set_run_state(states[1])

    assert [s.name for s in states] == ["default", "Night time"]
    assert states[0].active


async def test_saving_run_states_needs_api_v3(client: ZoneMinderLegacyClient) -> None:
    """API v2 can switch run states but not save them."""
    with pytest.raises(ZMNotSupportedError):
        await client.save_run_state("Holiday", "1:Modect:1")


async def test_v3_only_features_say_so(client: ZoneMinderLegacyClient) -> None:
    """The event stream and WebRTC exist on every client but need API v3."""
    with pytest.raises(ZMNotSupportedError):
        async for _message in client.async_event_stream():
            pass
    assert await client.async_supports_whep(1) is False
    with pytest.raises(ZMNotSupportedError):
        await client.async_whep_offer(1, "v=0")
    with pytest.raises(ZMNotSupportedError):
        await client.async_whep_candidate("/session", "0", "candidate:1")
    with pytest.raises(ZMNotSupportedError):
        await client.async_whep_close("/session")


async def test_reads_without_v3_return_nothing(client: ZoneMinderLegacyClient) -> None:
    """Reads API v2 can't do return empty results rather than failing."""
    assert await client.get_ptz_presets(1) == {}
    assert await client.async_get_rtsp_url(1) is None


async def test_status_and_summaries_come_with_the_monitors(
    client: ZoneMinderLegacyClient,
) -> None:
    """monitors.json carries Monitor_Status and Event_Summary for each monitor.

    One read answers the monitor list, the live status and the summaries, so
    a refresh costs no extra requests. A monitor zmc has never run for has a
    Monitor_Status of nulls and gets no status.
    """
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            v2(r"api/monitors\.json"),
            payload={
                "monitors": [
                    {
                        "Monitor": {"Id": 1, "Name": "Driveway", "Capturing": "Always"},
                        "Monitor_Status": {
                            "MonitorId": 1,
                            "Status": "Connected",
                            "CaptureFPS": "15.02",
                            "AnalysisFPS": "15.02",
                            "CaptureBandwidth": 3506,
                            "UpdatedOn": "2026-10-24 05:18:48",
                        },
                        "Event_Summary": {
                            "MonitorId": 1,
                            "TotalEvents": 14,
                            "DayEvents": 3,
                            "TotalEventDiskSpace": 51200,
                            "DayEventDiskSpace": 2048,
                        },
                    },
                    {
                        "Monitor": {"Id": 2, "Name": "Garage", "Capturing": "None"},
                        "Monitor_Status": {"MonitorId": None, "Status": None},
                        "Event_Summary": {"MonitorId": None},
                    },
                ]
            },
        )
        await client.get_monitors()
        statuses = await client.get_monitor_statuses()
        summary = await client.get_event_summary(1)
        assert await client.get_event_summary(2) is None
        assert len(sent(mocked, "GET", "api/monitors.json")) == 1

    assert list(statuses) == [1]
    assert statuses[1].connected is True
    assert statuses[1].capture_fps == 15.02
    assert statuses[1].capture_bandwidth == 3506
    assert summary is not None
    assert (summary.total_events, summary.day_events) == (14, 3)
    assert summary.total_disk_space == 51200


async def test_status_reads_the_monitors_when_stale(
    client: ZoneMinderLegacyClient,
) -> None:
    """Without a recent monitors.json read, status asks for one."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            v2(r"api/monitors\.json"),
            payload={
                "monitors": [
                    {
                        "Monitor": {"Id": 1, "Name": "Driveway"},
                        "Monitor_Status": {"MonitorId": 1, "Status": "NotRunning"},
                    }
                ]
            },
        )
        statuses = await client.get_monitor_statuses()
        assert len(sent(mocked, "GET", "api/monitors.json")) == 1
    assert statuses[1].connected is False


def v2(path: str) -> re.Pattern[str]:
    """Match an API v2 endpoint whatever its query string."""
    return re.compile(rf"^{re.escape(ZM)}/{path}(\?.*)?$")


def sent(mocked: aioresponses, method: str, path: str) -> list[Any]:
    """Return the recorded calls to one endpoint."""
    return [
        call
        for (verb, called), requests in mocked.requests.items()
        if verb == method and str(called).split("?")[0] == f"{ZM}/{path}"
        for call in requests
    ]


async def test_monitors(client: ZoneMinderLegacyClient) -> None:
    """Monitors parse from the StudlyCaps payload; broken ones are skipped."""
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(
            v2(r"api/monitors\.json"),
            payload={
                "monitors": [
                    {
                        "Monitor": {
                            "Id": "1",
                            "Name": "Driveway",
                            "Function": "Modect",
                            "Controllable": "1",
                        }
                    },
                    {"Monitor": {"Name": "No id"}},
                ]
            },
        )
        monitors = await client.get_monitors()

    (driveway,) = monitors
    assert driveway.name == "Driveway"
    assert (driveway.analysing, driveway.recording) == ("Always", "OnMotion")
    assert driveway.controllable


async def test_configs_and_server_info(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(v2(r"api/configs\.json"), payload=CONFIGS_RESPONSE)
        mocked.get(v2(r"api/host/getVersion\.json"), payload={"version": "1.36.33"})
        configs = await client.get_configs()
        info = await client.get_server_info()

    assert configs["ZM_MQTT_TOPIC_PREFIX"] == "ZoneMinder"
    assert info.version == "1.36.33"
    assert info.storage == []


async def test_alarms(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(v2(r"api/monitors/alarm/id:1/command:on\.json"), payload={})
        mocked.get(v2(r"api/monitors/alarm/id:1/command:off\.json"), payload={})
        await client.trigger_alarm(1, "Doorbell")
        await client.cancel_alarm(1)

        (on,) = sent(mocked, "GET", "api/monitors/alarm/id:1/command:on.json")
        assert on.kwargs["params"] == {"cause": "Doorbell", "token": "token"}
        assert sent(mocked, "GET", "api/monitors/alarm/id:1/command:off.json")


@pytest.mark.parametrize(
    ("command", "kwargs", "expected"),
    [
        ("up", {}, {"control": "moveConUp"}),
        ("zoom-in", {}, {"control": "zoomConTele"}),
        ("home", {}, {"control": "presetHome"}),
        ("preset", {"preset": 4}, {"control": "presetGoto", "preset": 4}),
        (
            "left",
            {"x": 10, "y": 20},
            {"control": "moveConLeft", "xCoord": 10, "yCoord": 20},
        ),
    ],
)
async def test_ptz(
    client: ZoneMinderLegacyClient,
    command: str,
    kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.post(v2(r"api/controls/3\.json"), payload={})
        await client.ptz(3, command, **kwargs)
        (call,) = sent(mocked, "POST", "api/controls/3.json")
    assert call.kwargs["data"] == expected


async def test_ptz_mistakes(client: ZoneMinderLegacyClient) -> None:
    with pytest.raises(ZMNotSupportedError):
        await client.ptz(3, "spin")
    with pytest.raises(ZMApiError, match="preset number"):
        await client.ptz(3, "preset")


async def test_not_found_and_server_errors(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(v2(r"api/monitors/9\.json"), status=404)
        mocked.get(v2(r"api/monitors/1\.json"), status=500)
        mocked.get(
            v2(r"api/monitors/2\.json"), exception=aiohttp.ServerDisconnectedError()
        )
        with pytest.raises(ZMApiError, match="Not found"):
            await client.get_monitor(9)
        with pytest.raises(ZMApiError, match="500"):
            await client.get_monitor(1)
        with pytest.raises(ZMConnectionError):
            await client.get_monitor(2)


async def test_unreachable_login(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        mocked.post(
            f"{ZM}/api/host/login.json", exception=aiohttp.ClientConnectionError()
        )
        with pytest.raises(ZMConnectionError):
            await client.authenticate()


async def test_snapshot(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        mocked.get(v2("cgi-bin/nph-zms"), body=b"\xff\xd8jpeg")
        mocked.get(v2("cgi-bin/nph-zms"), status=500)
        assert await client.async_get_snapshot(1) == b"\xff\xd8jpeg"
        assert await client.async_get_snapshot(1) is None


async def test_media_carries_the_token(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(v2(r"api/events/5\.mp4"), body=b"mp4")
        async with client.async_stream_media("api/events/5.mp4") as response:
            assert await response.read() == b"mp4"
        (call,) = sent(mocked, "GET", "api/events/5.mp4")
    assert call.kwargs["params"] == {"token": "token"}


async def test_urls_carry_the_token(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        await client.authenticate()
    assert client.get_stream_url(1).startswith(f"{ZM}/cgi-bin/nph-zms?mode=jpeg")
    assert client.get_stream_url(1).endswith("token=token")
    assert client.get_event_clip_url(5) == f"{ZM}/api/events/5.mp4?token=token"


async def test_close_logs_out(client: ZoneMinderLegacyClient) -> None:
    with aioresponses() as mocked:
        _login(mocked)
        mocked.get(v2(r"api/host/logout\.json"), payload={})
        await client.authenticate()
        await client.close()
        assert sent(mocked, "GET", "api/host/logout.json")
    assert not client.authenticated
