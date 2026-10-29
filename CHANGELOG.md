# Changelog

## 0.1.0 (unreleased)

First release, extracted from the zm-homeassistant Home Assistant integration.

- One async client interface over ZoneMinder API v3 (zm-api) and the built-in
  API v2, chosen with `create_client`.
- API v3: monitors, events, live status, event summaries, run states (switch
  and save), PTZ, alarms, snapshots, signed RTSP links, HLS, WebRTC over WHEP,
  and the pushed event stream.
- API v2: monitors, events, live status, event summaries, run states (switch),
  PTZ, alarms, snapshots, MJPEG with optional scale and frame rate limits, and
  go2rtc discovery. Handles multi-server setups, finds nph-zms wherever the
  package put it, and skips monitors ZoneMinder 1.38 has soft-deleted.
- `Monitor.function` names the six pre-1.37 modes as ZoneMinder defines them
  (Always/Always/OnMotion is Modect), so it agrees with `function_fields`.
- `get_alarm_state` reads a monitor's alarm state on both APIs: zm-api's
  live status, or zmu through API v2's alarm status, as zm-py does.
- PTZ moves, zoom and focus always send a speed, so cameras move instead of
  stopping.
- Features one API lacks raise `ZMNotSupportedError` or return an empty
  result, so the same code runs against both.
- Takes the caller's aiohttp session, refreshes tokens itself, and is fully
  typed.
- API v2 turned off (`OPT_USE_API`) raises `ZMConnectionError` naming the
  option, not `ZMAuthenticationError`, so it doesn't read as a wrong password.
- API v3 behind a proxy path (`http://host/zm`, zm-api's
  `server.public_prefix` set): paths zm-api writes with the prefix, such as a
  WHEP session's `Location`, no longer get it twice, so ICE candidates and
  session closes reach zm-api.
- Login errors say why when the server does: a user with API access turned
  off (API v2), or a user name zm-api refuses (API v3, 400), no longer reads
  as "Invalid username or password".
