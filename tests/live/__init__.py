"""Live tests: aiozoneminder against a real ZoneMinder server.

Everything else under ``tests/`` mocks the server. These tests call the
library's own methods against a server you point them at, over API v3 (zm-api),
API v2 (ZoneMinder's built-in API) or both, and judge the result through the
library or the API itself. They are skipped unless configured:

    ZM_LIVE_V2_URL        ZoneMinder's web path, e.g. http://zm.local/zm
    ZM_LIVE_V3_URL        zm-api's address, e.g. http://zm.local:8080
    ZM_LIVE_USER          a user with System and Monitors edit rights
    ZM_LIVE_PASSWORD
    ZM_LIVE_TIMEZONE      optional: the server's timezone, for API v2 times
    ZM_LIVE_MONITOR       optional: the monitor id to use; by default the first
                          capturing monitor, preferring a controllable one
    ZM_LIVE_RESTRICTED_USER, ZM_LIVE_RESTRICTED_PASSWORD
                          optional: a user without System view, for the
                          permission checks
    ZM_LIVE_WRITE=1       also run the tests that change the server

Every test runs once per configured API. Without ``ZM_LIVE_WRITE`` nothing on
the server is changed (live sessions are started, as any viewer would). With
it, the tests change monitor modes, capturing, alarms, PTZ and run states, and
put back what they changed: modes and the active run state are read back and
restored, alarms cancelled, and run states they create (named
``aiozm-live-*``) removed. Forced alarms leave their events behind, and the
token tests log out every session of ``ZM_LIVE_USER``, so use a dedicated
user and a test server.

Tests that guard a bug fixed by an open pull request carry
``xfail(strict=False)`` with the PR's title: they fail on a release without
the fix and pass with it.

Left out, because they need control of the server or the cameras that a test
can't assume:

- Camera motion: making a camera see motion needs a simulated camera. Forced
  alarms (``trigger_alarm``) stand in for it, for events, open events and the
  API v3 event stream.
- Whether PTZ moved the camera, or capturing stopped the capture daemon: only
  the camera or the server's process list can tell. The tests check that the
  library's call succeeds and what the API reports afterwards.
- Deleted monitors are checked only when the server already has one (deleting
  a monitor can't be undone through the API).
- Paging past 100 events is checked only when a monitor, or the server, has
  more than 100 events (a test can't add events).
- The event cause on API v2 is checked to be the cause sent or "Forced Web":
  ZoneMinder versions without ``zmu --cause`` record "Forced Web" whatever is
  sent, and the tests can't tell which version the server runs.
- Many requests refused together after a token revocation sharing one renewal:
  an unfixed client answers with a burst of logins, which zm-api's login rate
  limit turns into refusals for the tests that follow.
"""
