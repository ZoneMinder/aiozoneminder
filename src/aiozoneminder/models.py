"""Data models returned by the ZoneMinder clients.

Every model is backend-agnostic. ``from_v3_dict`` parses the standalone
zm-api v3 REST responses (snake_case); ``from_dict`` parses the legacy
CakePHP API responses (StudlyCaps).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from .const import ORIENTATION_DEGREES, ORIENTATION_LABELS


def _legacy_end(raw: dict[str, Any]) -> Any:
    """Return an API v2 event's end time, or None while it is still recording.

    From ZoneMinder 1.37 ``EndDateTime`` is the stored end, null while the
    event runs. From 1.38.3 ``EndTime`` is a computed field, ``StartDateTime``
    plus ``Length``, so it has a value while the event is still recording.
    ``EndTime`` only counts on servers older than 1.37, where it is the stored
    column and ``EndDateTime`` does not exist.
    """
    if "EndDateTime" in raw:
        return raw["EndDateTime"]
    return raw.get("EndTime")


def _parse_datetime(value: Any, tz: tzinfo | None = None) -> datetime | None:
    """Parse a ZoneMinder timestamp into a timezone-aware ``datetime``.

    zm-api returns ISO-8601 with a ``Z`` suffix. API v2 returns naive local
    time (``2024-01-01 12:00:00``), which is given ``tz``, or this machine's
    local timezone when ``tz`` is None.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
        except (ValueError, AttributeError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz or datetime.now().astimezone().tzinfo)
    return parsed


def _from_epoch(value: Any, tz: tzinfo | None = None) -> datetime | None:
    """Convert a Unix time to a ``datetime`` in ``tz``, or this machine's zone.

    Returns None when the value is missing or not a number.
    """
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC).astimezone(tz)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce a value to float, tolerating strings and ``None``."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, tolerating strings, floats and ``None``."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    """Coerce ZoneMinder's 0/1 integers and "0"/"1" strings to bool."""
    if isinstance(value, bool):
        return value
    return _to_int(value) != 0


def _derive_function(
    capturing: str | None, analysing: str | None, recording: str | None
) -> str:
    """Collapse the three ZoneMinder 1.37+ state fields into a legacy mode name.

    ZoneMinder split ``Function`` into ``Capturing``/``Analysing``/``Recording``.
    The server still reports a ``function`` field, but it does not always agree
    with the three real fields, so we derive it instead of trusting it.
    """
    if not capturing or capturing == "None":
        return "None"
    analyse = bool(analysing) and analysing != "None"
    record = bool(recording) and recording != "None"
    if analyse and record:
        return "Mocord"
    if analyse:
        return "Modect"
    if record:
        return "Record"
    return "Monitor"


@dataclass
class Monitor:
    """A ZoneMinder monitor (one camera)."""

    id: int
    name: str
    type: str
    width: int
    height: int

    # ZoneMinder 1.37+ splits the legacy Function into three fields.
    capturing: str = "None"
    analysing: str = "None"
    recording: str = "None"
    decoding: str = "None"

    orientation: str | None = None
    importance: str | None = None
    controllable: bool = False
    control_id: int | None = None
    mqtt_enabled: bool = False
    storage_id: int | None = None
    server_id: int | None = None
    protocol: str | None = None
    host: str | None = None
    path: str | None = None
    port: int | None = None
    notes: str | None = None

    @property
    def function(self) -> str:
        """Return the legacy-style mode derived from the three state fields."""
        return _derive_function(self.capturing, self.analysing, self.recording)

    @property
    def enabled(self) -> bool:
        """Return True when the monitor is capturing at all."""
        return self.capturing != "None"

    @property
    def is_recording(self) -> bool:
        """Return True when the monitor writes events."""
        return self.recording != "None"

    @property
    def is_analysing(self) -> bool:
        """Return True when the monitor runs motion analysis."""
        return self.analysing != "None"

    @property
    def orientation_label(self) -> str:
        """Return a human-readable orientation."""
        return ORIENTATION_LABELS.get(self.orientation or "", "Normal")

    @property
    def orientation_degrees(self) -> int:
        """Return the clockwise rotation in degrees, 0 for flips."""
        return ORIENTATION_DEGREES.get(self.orientation or "", 0)

    @classmethod
    def from_v3_dict(cls, data: dict[str, Any]) -> Monitor:
        """Build a Monitor from a zm-api v3 ``MonitorResponse``."""
        port = data.get("port")
        return cls(
            id=int(data["id"]),
            name=data["name"],
            type=data.get("type") or "",
            width=_to_int(data.get("width")),
            height=_to_int(data.get("height")),
            capturing=data.get("capturing") or "None",
            analysing=data.get("analysing") or "None",
            recording=data.get("recording") or "None",
            decoding=data.get("decoding") or "None",
            orientation=data.get("orientation"),
            importance=data.get("importance"),
            controllable=_to_bool(data.get("controllable")),
            control_id=_to_int(data["control_id"])
            if data.get("control_id") is not None
            else None,
            mqtt_enabled=_to_bool(data.get("mqtt_enabled")),
            storage_id=_to_int(data.get("storage_id")),
            server_id=_to_int(data.get("server_id")),
            protocol=data.get("protocol"),
            host=data.get("host"),
            path=data.get("path"),
            port=_to_int(port) if str(port or "").strip().isdigit() else None,
            notes=data.get("notes"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Monitor:
        """Build a Monitor from a legacy CakePHP API response.

        ZoneMinder 1.37+ reports both the split fields and the legacy
        ``Function``; older releases report only ``Function``. Prefer the split
        fields and fall back to expanding ``Function`` when absent.
        """
        raw = data.get("Monitor", data)

        capturing = raw.get("Capturing")
        analysing = raw.get("Analysing")
        recording = raw.get("Recording")
        if capturing is None:
            capturing, analysing, recording = _expand_function(raw.get("Function"))

        port = raw.get("Port")
        return cls(
            id=int(raw["Id"]),
            name=raw["Name"],
            type=raw.get("Type") or "",
            width=_to_int(raw.get("Width")),
            height=_to_int(raw.get("Height")),
            capturing=capturing or "None",
            analysing=analysing or "None",
            recording=recording or "None",
            decoding=raw.get("Decoding") or "None",
            orientation=raw.get("Orientation"),
            importance=raw.get("Importance"),
            controllable=_to_bool(raw.get("Controllable")),
            control_id=_to_int(raw["ControlId"])
            if raw.get("ControlId") is not None
            else None,
            mqtt_enabled=_to_bool(raw.get("MQTT_Enabled")),
            storage_id=_to_int(raw.get("StorageId")),
            server_id=_to_int(raw.get("ServerId")),
            protocol=raw.get("Protocol"),
            host=raw.get("Host"),
            path=raw.get("Path"),
            port=_to_int(port) if str(port or "").strip().isdigit() else None,
            notes=raw.get("Notes"),
        )


#: Each pre-1.37 ``Function`` as (capturing, analysing, recording).
_FUNCTION_FIELDS: dict[str, tuple[str, str, str]] = {
    "None": ("None", "None", "None"),
    "Monitor": ("Always", "None", "None"),
    "Modect": ("Always", "Always", "OnMotion"),
    "Record": ("Always", "None", "Always"),
    "Mocord": ("Always", "Always", "Always"),
    "Nodect": ("Always", "None", "OnMotion"),
}


def function_fields(function: str) -> tuple[str, str, str]:
    """Return a pre-1.37 function, such as Modect, as (capturing, analysing, recording).

    For callers that still think in the old single mode, like Home
    Assistant's core integration did. Pass the result to
    ``set_monitor_state``.
    """
    try:
        return _FUNCTION_FIELDS[function]
    except KeyError:
        raise ValueError(
            f"Unknown function {function!r}; use one of {list(_FUNCTION_FIELDS)}"
        ) from None


def _expand_function(function: str | None) -> tuple[str, str, str]:
    """Expand an API v2 ``Function``, treating a missing one as None."""
    return _FUNCTION_FIELDS.get(function or "None", _FUNCTION_FIELDS["None"])


@dataclass
class MonitorStatus:
    """Live capture statistics for a monitor, from ``/monitor-status``."""

    monitor_id: int
    status: str = "Unknown"
    capture_fps: float = 0.0
    analysis_fps: float = 0.0
    capture_bandwidth: int = 0
    updated_on: datetime | None = None

    @property
    def connected(self) -> bool:
        """Return True when ZoneMinder reports the capture daemon connected."""
        return self.status == "Connected"

    @classmethod
    def from_v3_dict(cls, data: dict[str, Any]) -> MonitorStatus:
        """Build a MonitorStatus from a zm-api v3 ``MonitorStatusResponse``."""
        return cls(
            monitor_id=int(data["monitor_id"]),
            status=data.get("status") or "Unknown",
            capture_fps=_to_float(data.get("capture_fps")),
            analysis_fps=_to_float(data.get("analysis_fps")),
            capture_bandwidth=_to_int(data.get("capture_bandwidth")),
            updated_on=_parse_datetime(data.get("updated_on")),
        )

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], tz: tzinfo | None = None
    ) -> MonitorStatus | None:
        """Build from an API v2 ``Monitor_Status`` row; None when it's empty.

        A monitor ZoneMinder has never started has no row, which the API
        returns as a row of nulls.
        """
        if data.get("MonitorId") is None:
            return None
        return cls(
            monitor_id=int(data["MonitorId"]),
            status=data.get("Status") or "Unknown",
            capture_fps=_to_float(data.get("CaptureFPS")),
            analysis_fps=_to_float(data.get("AnalysisFPS")),
            capture_bandwidth=_to_int(data.get("CaptureBandwidth")),
            updated_on=_parse_datetime(data.get("UpdatedOn"), tz),
        )


@dataclass
class Event:
    """A ZoneMinder event (one recorded clip)."""

    id: int
    monitor_id: int
    name: str
    cause: str
    start_time: datetime
    end_time: datetime | None = None
    length: float = 0.0
    frames: int = 0
    alarm_frames: int = 0
    max_score: int = 0
    avg_score: int = 0
    total_score: int = 0
    disk_space: int | None = None
    default_video: str | None = None
    archived: bool = False
    notes: str | None = None
    width: int = 0
    height: int = 0

    @property
    def in_progress(self) -> bool:
        """Return True while ZoneMinder is still writing this event."""
        return self.end_time is None

    # Kept for backwards compatibility with earlier releases.
    is_active = in_progress

    @property
    def is_alarm(self) -> bool:
        """Return True when this event contains actual motion.

        Continuous recording produces a steady stream of events with no alarm
        frames. Only events with alarmed frames represent detected motion.
        """
        return self.alarm_frames > 0

    @property
    def has_video(self) -> bool:
        """Return True when a finished, playable clip exists."""
        return bool(self.default_video) and not self.in_progress

    @classmethod
    def from_v3_dict(cls, data: dict[str, Any]) -> Event:
        """Build an Event from a zm-api v3 ``EventResponse``."""
        start_time = _parse_datetime(data.get("start_date_time"))
        if start_time is None:
            raise ValueError("Event is missing a start time")
        return cls(
            id=int(data["id"]),
            monitor_id=int(data["monitor_id"]),
            name=data.get("name") or "",
            cause=data.get("cause") or "",
            start_time=start_time,
            end_time=_parse_datetime(data.get("end_date_time")),
            length=_to_float(data.get("length")),
            frames=_to_int(data.get("frames")),
            alarm_frames=_to_int(data.get("alarm_frames")),
            max_score=_to_int(data.get("max_score")),
            avg_score=_to_int(data.get("avg_score")),
            total_score=_to_int(data.get("tot_score")),
            disk_space=_to_int(data["disk_space"])
            if data.get("disk_space") is not None
            else None,
            default_video=data.get("default_video"),
            archived=_to_bool(data.get("archived")),
            notes=data.get("notes"),
            width=_to_int(data.get("width")),
            height=_to_int(data.get("height")),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], tz: tzinfo | None = None) -> Event:
        """Build an Event from an API v2 response.

        API v2 times carry no timezone; ``tz`` says which one they're in. The
        Unix times ``StartTimeSecs`` and ``EndTimeSecs`` need no zone, so they
        win when the server sends them.
        """
        raw = data.get("Event", data)
        start_time = _from_epoch(raw.get("StartTimeSecs"), tz) or _parse_datetime(
            raw.get("StartTime") or raw.get("StartDateTime"), tz
        )
        if start_time is None:
            raise ValueError("Event is missing a start time")
        # EndTime is computed for open events on 1.38.3+; _legacy_end knows.
        end_time = _parse_datetime(_legacy_end(raw), tz)
        if end_time is not None:
            # EndTimeSecs is computed for open events too, so it only gives the
            # value of an end the event is known to have.
            end_time = _from_epoch(raw.get("EndTimeSecs"), tz) or end_time
        return cls(
            id=int(raw["Id"]),
            monitor_id=int(raw["MonitorId"]),
            name=raw.get("Name") or "",
            cause=raw.get("Cause") or "",
            start_time=start_time,
            end_time=end_time,
            length=_to_float(raw.get("Length")),
            frames=_to_int(raw.get("Frames")),
            alarm_frames=_to_int(raw.get("AlarmFrames")),
            max_score=_to_int(raw.get("MaxScore")),
            avg_score=_to_int(raw.get("AvgScore")),
            total_score=_to_int(raw.get("TotScore")),
            disk_space=_to_int(raw["DiskSpace"])
            if raw.get("DiskSpace") is not None
            else None,
            default_video=raw.get("DefaultVideo"),
            archived=_to_bool(raw.get("Archived")),
            notes=raw.get("Notes"),
            width=_to_int(raw.get("Width")),
            height=_to_int(raw.get("Height")),
        )


@dataclass
class EventSummary:
    """Per-monitor event counts and disk usage, from ``/event-summaries``."""

    monitor_id: int
    total_events: int = 0
    hour_events: int = 0
    day_events: int = 0
    week_events: int = 0
    month_events: int = 0
    archived_events: int = 0
    total_disk_space: int = 0
    day_disk_space: int = 0

    @classmethod
    def from_v3_dict(cls, data: dict[str, Any]) -> EventSummary:
        """Build an EventSummary from a zm-api v3 response."""
        return cls(
            monitor_id=int(data["monitor_id"]),
            total_events=_to_int(data.get("total_events")),
            hour_events=_to_int(data.get("hour_events")),
            day_events=_to_int(data.get("day_events")),
            week_events=_to_int(data.get("week_events")),
            month_events=_to_int(data.get("month_events")),
            archived_events=_to_int(data.get("archived_events")),
            total_disk_space=_to_int(data.get("total_event_disk_space")),
            day_disk_space=_to_int(data.get("day_event_disk_space")),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventSummary | None:
        """Build from an API v2 ``Event_Summary`` row; None when it's empty."""
        if data.get("MonitorId") is None:
            return None
        return cls(
            monitor_id=int(data["MonitorId"]),
            total_events=_to_int(data.get("TotalEvents")),
            hour_events=_to_int(data.get("HourEvents")),
            day_events=_to_int(data.get("DayEvents")),
            week_events=_to_int(data.get("WeekEvents")),
            month_events=_to_int(data.get("MonthEvents")),
            archived_events=_to_int(data.get("ArchivedEvents")),
            total_disk_space=_to_int(data.get("TotalEventDiskSpace")),
            day_disk_space=_to_int(data.get("DayEventDiskSpace")),
        )


@dataclass
class StorageArea:
    """A ZoneMinder storage area."""

    id: int
    name: str
    path: str
    disk_space: int = 0
    enabled: bool = True
    type: str = "local"

    @classmethod
    def from_v3_dict(cls, data: dict[str, Any]) -> StorageArea:
        """Build a StorageArea from a zm-api v3 ``StorageResponse``."""
        return cls(
            id=int(data["id"]),
            name=data.get("name") or "",
            path=data.get("path") or "",
            disk_space=_to_int(data.get("disk_space")),
            enabled=_to_bool(data.get("enabled")),
            type=data.get("type") or "local",
        )


#: What ZoneMinder's old single "Function" means in the split fields, exactly
#: as zmpkg.pl applies it when switching run state.
_RUN_STATE_FUNCTIONS: dict[str, tuple[str, str, str]] = {
    "None": ("None", "None", "None"),
    "Monitor": ("Always", "None", "None"),
    "Modect": ("Always", "Always", "OnMotion"),
    "Mocord": ("Always", "Always", "OnMotion"),
    "Record": ("Always", "Always", "Always"),
}


@dataclass
class RunState:
    """A ZoneMinder run state: a saved set of modes for every monitor."""

    id: int
    name: str
    definition: str
    active: bool

    def monitor_modes(self) -> dict[int, tuple[str, str, str]]:
        """Return (capturing, analysing, recording) per monitor id.

        Definitions are "id:Capturing:Analysing:Recording" per monitor from
        ZoneMinder 1.37 on, and "id:Function:Enabled" before that.
        """
        modes: dict[int, tuple[str, str, str]] = {}
        for part in self.definition.split(","):
            fields = part.strip().split(":")
            if not fields[0].isdigit():
                continue
            monitor_id = int(fields[0])
            if len(fields) == 4:
                modes[monitor_id] = (fields[1], fields[2], fields[3])
            elif len(fields) == 3:
                function, enabled = fields[1], fields[2]
                modes[monitor_id] = (
                    _RUN_STATE_FUNCTIONS["None"]
                    if enabled in ("0", "")
                    else _RUN_STATE_FUNCTIONS.get(
                        function, _RUN_STATE_FUNCTIONS["Mocord"]
                    )
                )
        return modes

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        """Build from either API: v3 snake_case or v2 CamelCase."""
        return cls(
            id=_to_int(data.get("id", data.get("Id"))),
            name=str(data.get("name", data.get("Name")) or ""),
            definition=str(data.get("definition", data.get("Definition")) or ""),
            active=_to_bool(data.get("is_active", data.get("IsActive"))),
        )


@dataclass
class ServerInfo:
    """Server-wide state: version, daemons and storage.

    ``running`` is None when the server did not say, for instance because the
    user may not view system status or the API has no such report.
    """

    version: str = "unknown"
    api_version: str | None = None
    db_version: str | None = None
    running: bool | None = None
    daemons_running: int = 0
    daemons_total: int = 0
    storage: list[StorageArea] = field(default_factory=list)

    @property
    def total_disk_space(self) -> int:
        """Return bytes used across all enabled storage areas."""
        return sum(area.disk_space for area in self.storage if area.enabled)
