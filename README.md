# aiozoneminder

An async Python client for [ZoneMinder](https://zoneminder.com). It talks to
either of ZoneMinder's APIs through the same methods:

- **API v3**: [zm-api](https://github.com/SteveGilvarry/zm-api), the standalone
  REST server. It adds WebRTC, a pushed event stream, signed RTSP links and
  event summaries.
- **API v2**: the CakePHP API built into ZoneMinder (`/zm/api`). Works on any
  ZoneMinder, with fewer features.

It's built on aiohttp and takes your session rather than making its own, so it
fits into an existing asyncio application. It powers the
[zm-homeassistant](https://github.com/SteveGilvarry/zm-homeassistant) Home
Assistant integration, but doesn't depend on Home Assistant.

## Install

```bash
pip install aiozoneminder
```

Needs Python 3.13 or newer.

## Example

```python
import asyncio

import aiohttp
from aiozoneminder import create_client


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        zm = create_client("v3", "http://zm.local:8080", "admin", "secret", session)
        await zm.authenticate()

        for monitor in await zm.get_monitors():
            print(monitor.id, monitor.name, monitor.recording)

        # API v3 pushes event starts and ends as they happen.
        async for kind, data in zm.async_event_stream():
            print(kind, data)


asyncio.run(main())
```

For API v2, pass `"legacy"` and ZoneMinder's web address instead, such as
`http://zm.local/zm`.

## What each API can do

| | API v3 (zm-api) | API v2 (built in) |
|---|---|---|
| Monitors, events, configs | Yes | Yes |
| Change capturing, analysing, recording, decoding | Yes | Yes |
| Trigger and cancel alarms | Yes | Yes |
| PTZ: move, zoom, focus, home, presets | Yes | Yes |
| Snapshots | Yes | Yes |
| Switch run states | Yes | Yes |
| Save run states | Yes | No |
| Live status: capture rate, bandwidth | Yes | Yes |
| Live alarm state | Yes | No |
| Event summaries (counts and disk use per monitor) | Yes | Yes |
| Pushed event stream | Yes | No: poll `get_events` |
| WebRTC playback (WHEP) | Yes | No |
| Live video | HLS, signed RTSP links | MJPEG, or go2rtc if ZoneMinder has it |

Where an API lacks a feature, reads return an empty result and actions raise
`ZMNotSupportedError`, so the same code runs against both.

## Things to know

- **Sessions.** The client uses the aiohttp session you pass in and never
  closes it.
- **Logging in.** Call `authenticate()` once. Tokens are refreshed for you.
- **The event stream ends** when its token expires, about every ten minutes.
  Reconnect with `since=` set to the last event id you saw, and you won't miss
  anything in between.
- **Timezones.** API v2 sends times without a timezone. Pass `timezone=` to
  `create_client` to say which one ZoneMinder uses; the default is this
  machine's. API v3 times already carry one.
- **Errors.** Everything raised derives from `ZMError`:
  `ZMConnectionError`, `ZMAuthenticationError`, `ZMApiError` (with
  `ZMNotFoundError`) and `ZMNotSupportedError`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test,lint]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/mypy
```

## Credits

[zm-py](https://github.com/rohankapoorcom/zm-py), by Rohan Kapoor, Nic Boet and
others, is the synchronous client behind Home Assistant's built-in ZoneMinder
integration. aiozoneminder is a separate, async codebase, but zm-py showed the
way through ZoneMinder's API v2 quirks.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

Contributions need a signed [CLA](CLA.md); see [CONTRIBUTING](CONTRIBUTING.md).
