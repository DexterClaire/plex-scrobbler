# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
# Run interactively (for testing/debugging)
python plex_scrobbler.py

# Run in debug mode as a foreground service
python plex_scrobbler.py debug
```

## Windows Service commands (requires admin prompt)

```bash
python plex_scrobbler.py install
python plex_scrobbler.py start
python plex_scrobbler.py stop
python plex_scrobbler.py restart
python plex_scrobbler.py remove
```

## Dependencies

```bash
pip install requests pywin32
python -m pip install --upgrade pywin32
python C:\Users\USERNAME\AppData\Local\Programs\Python\Python311\Scripts\pywin32_postinstall.py -install
```

## Architecture

Single-file script (`plex_scrobbler.py`). The main execution flow:

1. **Config loading** — `load_config()` reads `config.ini` at module level on startup. If missing, writes a template and exits.
2. **`run()`** — core loop: calls `tail_log()` and feeds each line to `process_line()`. Watches `sources.json` mtime and hot-reloads on change.
3. **`tail_log(path)`** — generator that yields lines from the Plex log forever. Handles log rotation by detecting file shrinkage (Windows has no inode, so `(size, ctime)` is used as file identity).
4. **`process_line(line, sources)`** — matches each line against three regexes:
   - `RE_TIMELINE` — client timeline state updates; tracks current `ratingKey` and session state per `req` ID.
   - `RE_DEVICE` — maps a `req` ID to a device string (e.g. `iOS (iPhone)`); triggers Now Playing if the session is already in `playing` state.
   - `RE_SCROBBLE` — fires when Plex logs its own scrobble signal (≈50% playback); calls `lastfm_scrobble()` if the source is enabled.
5. **Session state** — `sessions` dict keyed by Plex request ID (`req`). `client_to_req` maps client UUID → req. `_now_playing_sent` is a **global** set of `(client, ratingKey)` tuples — not a per-session flag — because each Plex heartbeat arrives on a new `req` ID, so any flag stored on `sessions[req]` would be lost between heartbeats. Keying on the stable `(client, ratingKey)` pair survives across req IDs for the same track. The entry is discarded when `ratingKey` changes for a client (track change).
6. **Windows Service** — `PlexScrobblerService` wraps `run()` in a daemon thread. `pywin32` import is wrapped in try/except so the script still works without it.

## Key files

| File | Purpose |
|---|---|
| `plex_scrobbler.py` | Entire application |
| `config.ini` | Credentials and paths — not committed |
| `config.ini.template` | Committed safe template |
| `sources.json` | Per-source enable/disable, hot-reloaded at runtime |

## Source matching

`source_enabled_for_device(device_str, sources)` does a case-insensitive substring match of each source's `match` value against the device string Plex logs. To discover the device string for a new source, check `plex_scrobbler.log` for lines like:

```
Unknown source: ' (WiiM Ultra)' — not scrobbling
```

Sources are configured in `sources.json`; the file is watched for changes and reloads automatically without a restart.
