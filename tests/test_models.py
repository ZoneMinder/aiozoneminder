"""Tests for the ZoneMinder data models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aiozoneminder import function_fields
from aiozoneminder.models import Event, Monitor, MonitorStatus

from .fixtures import event as make_event, monitor as make_monitor


def test_monitor_derives_function_from_split_fields() -> None:
    """ZoneMinder 1.39 reports a stale Function; the split fields are the truth."""
    # The live server reports function="Monitor" for this exact combination.
    data = make_monitor(1, "Driveway", analysing="None", recording="Always")
    data["function"] = "Monitor"

    parsed = Monitor.from_v3_dict(data)

    assert parsed.function == "Record"
    assert parsed.is_recording is True
    assert parsed.is_analysing is False
    assert parsed.enabled is True


@pytest.mark.parametrize(
    ("capturing", "analysing", "recording", "expected"),
    [
        ("None", "None", "None", "None"),
        ("Always", "None", "None", "Monitor"),
        ("Always", "Always", "OnMotion", "Modect"),
        ("Always", "None", "Always", "Record"),
        ("Always", "Always", "Always", "Mocord"),
        ("Always", "None", "OnMotion", "Nodect"),
        # No pre-1.37 mode has these; the nearest name.
        ("Always", "Always", "None", "Modect"),
        ("Ondemand", "None", "None", "Monitor"),
    ],
)
def test_function_matrix(
    capturing: str, analysing: str, recording: str, expected: str
) -> None:
    """Every combination of the three state fields maps to a legacy mode."""
    parsed = Monitor.from_v3_dict(
        make_monitor(
            1, "x", capturing=capturing, analysing=analysing, recording=recording
        )
    )
    assert parsed.function == expected


@pytest.mark.parametrize(
    "function", ["None", "Monitor", "Modect", "Record", "Mocord", "Nodect"]
)
def test_function_round_trips(function: str) -> None:
    """A monitor set to a mode with function_fields reads back as that mode."""
    capturing, analysing, recording = function_fields(function)
    parsed = Monitor.from_v3_dict(
        make_monitor(
            1, "x", capturing=capturing, analysing=analysing, recording=recording
        )
    )
    assert parsed.function == function


@pytest.mark.parametrize(
    ("raw", "label", "degrees"),
    [
        ("Rotate0", "Normal", 0),
        ("Rotate90", "Rotate right", 90),
        ("Rotate270", "Rotate left", 270),
        ("FlipHori", "Flipped horizontally", 0),
        # ZoneMinder 1.36 spelling.
        ("ROTATE_180", "Inverted", 180),
        (None, "Normal", 0),
    ],
)
def test_orientation(raw: str | None, label: str, degrees: int) -> None:
    """Orientation uses ZoneMinder 1.39's spelling, not the 1.36 one."""
    parsed = Monitor.from_v3_dict(make_monitor(1, "x", orientation=raw or "Rotate0"))
    parsed.orientation = raw
    assert parsed.orientation_label == label
    assert parsed.orientation_degrees == degrees


def test_legacy_monitor_expands_function() -> None:
    """A 1.36 monitor with only Function still yields the split fields."""
    parsed = Monitor.from_dict(
        {
            "Monitor": {
                "Id": "4",
                "Name": "Shed",
                "Function": "Modect",
                "Type": "Ffmpeg",
                "Width": "1280",
                "Height": "720",
                "Enabled": "1",
            }
        }
    )
    assert parsed.capturing == "Always"
    assert parsed.analysing == "Always"
    assert parsed.is_analysing is True
    assert parsed.function == "Modect"


def test_event_timestamps_are_timezone_aware() -> None:
    """A naive timestamp would make a TIMESTAMP sensor invalid."""
    parsed = Event.from_v3_dict(make_event(1, 1))
    assert parsed.start_time.tzinfo is not None
    assert parsed.start_time == datetime(2026, 8, 22, 13, 44, 52, tzinfo=UTC)


def test_legacy_event_timestamp_gets_local_timezone() -> None:
    """The legacy API returns naive local times, which need anchoring."""
    parsed = Event.from_dict(
        {
            "Event": {
                "Id": "9",
                "MonitorId": "1",
                "Name": "Event",
                "Cause": "Motion",
                "StartTime": "2026-08-22 13:44:52",
                "EndTime": "",
                "Length": "12.5",
                "Frames": "125",
                "AlarmFrames": "9",
            }
        }
    )
    assert parsed.start_time.tzinfo is not None
    assert parsed.end_time is None
    assert parsed.in_progress is True
    assert parsed.is_alarm is True


def test_open_legacy_event_is_in_progress() -> None:
    """ZoneMinder 1.37+ fills EndTime with the current time while recording.

    EndDateTime is null until the event closes, and that is what counts.
    """
    raw = {
        "Event": {
            "Id": 17,
            "MonitorId": 3,
            "Cause": "Forced Web",
            "StartTime": "2026-10-24 05:23:49",
            "StartDateTime": "2026-10-24 05:23:49",
            "EndTime": "2026-10-24 05:23:53",
            "EndDateTime": None,
            "AlarmFrames": 1,
        }
    }
    parsed = Event.from_dict(raw, UTC)
    assert parsed.end_time is None
    assert parsed.in_progress is True

    raw["Event"]["EndDateTime"] = "2026-10-24 05:24:03"
    raw["Event"]["EndTime"] = "2026-10-24 05:24:03"
    closed = Event.from_dict(raw, UTC)
    assert closed.in_progress is False
    assert closed.end_time == datetime(2026, 10, 24, 5, 24, 3, tzinfo=UTC)


def test_legacy_event_prefers_unix_times() -> None:
    """StartTimeSecs/EndTimeSecs carry no zone, so they beat the naive strings.

    A ZoneMinder container on UTC and Home Assistant in New York used to shift
    every event by the difference between the two zones.
    """
    new_york = timezone(timedelta(hours=-4))
    parsed = Event.from_dict(
        {
            "Event": {
                "Id": "9",
                "MonitorId": "1",
                # Server-local (UTC) wall time, read as New York without the epoch.
                "StartDateTime": "2026-10-24 05:36:48",
                "EndDateTime": "2026-10-24 05:37:00",
                "StartTimeSecs": 1792820208,
                "EndTimeSecs": "1792820220.50",
            }
        },
        new_york,
    )
    assert parsed.start_time == datetime(2026, 10, 24, 5, 36, 48, tzinfo=UTC)
    assert parsed.start_time.utcoffset() == timedelta(hours=-4)
    assert parsed.end_time == datetime(2026, 10, 24, 5, 37, 0, 500000, tzinfo=UTC)


def test_legacy_open_event_ignores_computed_end_secs() -> None:
    """EndTimeSecs is computed for a running event; it must not close it."""
    parsed = Event.from_dict(
        {
            "Event": {
                "Id": "9",
                "MonitorId": "1",
                "StartDateTime": "2026-10-24 05:36:48",
                # ZoneMinder 1.38.3+ computes EndTime for a running event.
                "EndTime": "2026-10-24 05:36:48",
                "EndDateTime": None,
                "StartTimeSecs": 1792820208,
                "EndTimeSecs": "1792820208",
            }
        }
    )
    assert parsed.end_time is None


def test_continuous_event_is_not_motion() -> None:
    """Continuous recording opens events constantly; none of them are motion."""
    parsed = Event.from_v3_dict(make_event(1, 1, cause="Continuous", alarm_frames=0))
    assert parsed.is_alarm is False


def test_motion_event_is_motion() -> None:
    """An event with alarmed frames is motion."""
    parsed = Event.from_v3_dict(make_event(1, 2, cause="Motion", alarm_frames=17))
    assert parsed.is_alarm is True
    assert parsed.max_score == 42


def test_event_without_finished_video_is_not_playable() -> None:
    """An event still being written has nothing to play yet."""
    parsed = Event.from_v3_dict(
        make_event(1, 1, end=None, default_video="incomplete.h264.mp4")
    )
    assert parsed.in_progress is True
    assert parsed.has_video is False


def test_event_tolerates_null_disk_space() -> None:
    """In-progress events report a null disk_space."""
    raw = make_event(1, 1)
    raw["disk_space"] = None
    assert Event.from_v3_dict(raw).disk_space is None


def test_event_requires_a_start_time() -> None:
    """An event with no start time cannot be placed on a timeline."""
    raw = make_event(1, 1)
    raw["start_date_time"] = None
    with pytest.raises(ValueError, match="start time"):
        Event.from_v3_dict(raw)


def test_monitor_status_parses_string_numbers() -> None:
    """zm-api returns the frame rates as strings."""
    parsed = MonitorStatus.from_v3_dict(
        {
            "monitor_id": 1,
            "status": "Connected",
            "capture_fps": "10.94",
            "analysis_fps": "0.00",
            "capture_bandwidth": 1474209,
            "updated_on": "2026-08-22T13:52:54+00:00",
        }
    )
    assert parsed.capture_fps == pytest.approx(10.94)
    assert parsed.connected is True


@pytest.mark.parametrize(
    ("function", "fields"),
    [
        ("None", ("None", "None", "None")),
        ("Monitor", ("Always", "None", "None")),
        ("Modect", ("Always", "Always", "OnMotion")),
        ("Record", ("Always", "None", "Always")),
        ("Mocord", ("Always", "Always", "Always")),
        ("Nodect", ("Always", "None", "OnMotion")),
    ],
)
def test_function_fields(function: str, fields: tuple[str, str, str]) -> None:
    """Each pre-1.37 function is a fixed (capturing, analysing, recording)."""
    assert function_fields(function) == fields


def test_function_fields_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="Motion"):
        function_fields("Motion")
