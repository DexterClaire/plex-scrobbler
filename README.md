# Plex Last.fm Scrobbler

Scrobbles music from your Plex Media Server to Last.fm — but only from the playback sources you choose.

Plex's built-in Last.fm scrobbling submits every play regardless of where it came from. This means if you use multiple apps or devices (Plexamp, a WiiM streamer, the web player), and some of those already have their own scrobblers, you end up with duplicates. This script solves that by reading the Plex server log in real-time, identifying the playback source per session, and selectively scrobbling only from sources you have enabled.

## Features

- Per-source scrobble enable/disable (Plexamp iOS, Plex Web, WiiM, etc.)
- Last.fm **Now Playing** updates as soon as a track starts
- Scrobbles at Plex's native 50% threshold
- Source config hot-reloads from `sources.json` without a restart
- Survives Plex log rotation without restarting
- Rotating log file (15 MB max)
- Runs as a Windows Service

## Requirements

- Windows
- Python 3.11+
- A [Last.fm API account](https://www.last.fm/api/account/create)
- `pip install requests pywin32`

## Installation

**1. Clone the repo**

```
git clone https://github.com/DexterClaire/plex-scrobbler.git
cd plex-scrobbler
```

**2. Install dependencies**

```
pip install requests pywin32
python -m pip install --upgrade pywin32
python C:\Users\USERNAME\AppData\Local\Programs\Python\Python311\Scripts\pywin32_postinstall.py -install
```

**3. Create your config**

Run the script once to generate a `config.ini` template:

```
python plex_scrobbler.py
```

This creates `config.ini` in the same folder and exits. Fill in your values:

```ini
[plex]
url   = http://localhost:32400
token = YOUR_PLEX_TOKEN
log   = C:\Users\USERNAME\AppData\Local\Plex Media Server\Logs\Plex Media Server.log

[lastfm]
api_key      = YOUR_LASTFM_API_KEY
api_secret   = YOUR_LASTFM_API_SECRET
username     = YOUR_LASTFM_USERNAME
password_md5 = YOUR_PASSWORD_MD5_HASH

[scrobbler]
poll_seconds = 2
log_max_mb   = 5
log_backups  = 3
```

To get your Plex token: open Plex Web, play something, open browser dev tools → Network tab, find any request to your server and look for `X-Plex-Token` in the query string.

To generate your Last.fm password MD5 hash:

```
python -c "import hashlib; print(hashlib.md5(b'yourpassword').hexdigest())"
```

**4. Configure sources**

Edit `sources.json` to enable or disable scrobbling per source. The script creates this file with defaults on first run:

```json
{
  "Plexamp iOS": {
    "match": "iOS (iPhone)",
    "enabled": true
  },
  "Plex Web": {
    "match": "Chrome (Chrome)",
    "enabled": false
  },
  "WiiM Ultra": {
    "match": "WiiM Ultra",
    "enabled": false
  }
}
```

The `match` value is a substring matched against the device string Plex logs for each session. To discover the device string for a new source, play something from it and check `plex_scrobbler.log` for a line like:

```
Unknown source: ' (WiiM Ultra)' — not scrobbling
```

Add a new entry to `sources.json` with that string as the match value. The file is watched for changes and reloads automatically — no restart needed.

**5. Install as a Windows Service**

From an administrator command prompt in the script folder:

```
python plex_scrobbler.py install
python plex_scrobbler.py start
```

The service starts automatically on Windows boot. Use `restart_scrobbler.bat` (run as administrator) to stop and start it quickly.

Other service commands:

```
python plex_scrobbler.py stop
python plex_scrobbler.py restart
python plex_scrobbler.py remove
python plex_scrobbler.py debug    # run interactively for testing
```

## Files

| File | Description |
|---|---|
| `plex_scrobbler.py` | Main script |
| `config.ini` | Your credentials and paths — **not committed to git** |
| `config.ini.template` | Safe template to show config structure |
| `sources.json` | Per-source scrobble enable/disable |
| `plex_scrobbler.log` | Rolling log — **not committed to git** |
| `restart_scrobbler.bat` | Convenience script to restart the service |

## How it works

Plex Media Server logs a `Device is` line for every playback session that identifies the client (e.g. `Device is iOS (iPhone)`). The script tails the log in real time, maps that string to a named source via `sources.json`, and either submits the scrobble to Last.fm or suppresses it based on whether that source is enabled.

Now Playing is sent as soon as the device is identified on a new track. The scrobble fires when Plex logs its own internal scrobble signal, which occurs at approximately 50% of the track duration.

## License

MIT
