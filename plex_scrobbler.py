"""
Plex Last.fm Scrobbler
Watches Plex Media Server logs in real-time, identifies playback source,
and selectively scrobbles to Last.fm based on a per-source config.

All configuration lives in config.ini next to this script.
Do not edit credentials in this file.
"""

# ─── Fixed constants (not user-configurable) ──────────────────────────────────
SOURCES_FILE         = "sources.json"
CONFIG_INI           = "config.ini"
SCROBBLER_LOG_FILE   = "plex_scrobbler.log"
SERVICE_NAME         = "PlexScrobbler"
SERVICE_DISPLAY_NAME = "Plex Last.fm Scrobbler"
SERVICE_DESCRIPTION  = "Watches Plex Media Server logs and scrobbles to Last.fm based on playback source."

DEFAULT_SOURCES = {
    "Plexamp iOS":   {"match": "iOS (iPhone)",   "enabled": True},
    "Plex Mobile":   {"match": "iOS (iPad)",      "enabled": True},
    "Plex Web":      {"match": "Chrome (Chrome)", "enabled": False},
    "WiiM Ultra":    {"match": "WiiM Ultra",      "enabled": False},
    "WiiM Home App": {"match": "WiiM",            "enabled": False},
}

# ══════════════════════════════════════════════════════════════════════════════
# No edits needed below this line
# ══════════════════════════════════════════════════════════════════════════════

import re
import time
import json
import hashlib
import os
import sys
import logging
import configparser
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ─── Config loader ────────────────────────────────────────────────────────────

_CONFIG_TEMPLATE = """\
[plex]
url   = http://localhost:32400
token = XXXXXXXXXXXXXXXXXXXX
log   = C:\\Users\\willr\\AppData\\Local\\Plex Media Server\\Logs\\Plex Media Server.log

[lastfm]
api_key      = XXXXXXXXXXXXXXXXXXXX
api_secret   = XXXXXXXXXXXXXXXXXXXX
username     = XXXXXXXXXXXXXXXXXXXX
; md5 hash of your password
; python -c "import hashlib; print(hashlib.md5(b'yourpassword').hexdigest())"
password_md5 = XXXXXXXXXXXXXXXXXXXX

[scrobbler]
poll_seconds   = 0.25
log_max_mb     = 5
log_backups    = 3
"""

def load_config():
    """Read config.ini, creating a template if it doesn't exist."""
    config_path = Path(__file__).parent / CONFIG_INI
    if not config_path.exists():
        config_path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        print(f"Created {CONFIG_INI} — fill in your credentials then restart.")
        sys.exit(0)

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    missing = []
    for section, key in [
        ("plex",   "token"),
        ("lastfm", "api_key"),
        ("lastfm", "api_secret"),
        ("lastfm", "username"),
        ("lastfm", "password_md5"),
    ]:
        val = cfg.get(section, key, fallback="")
        if not val or val.startswith("XXX"):
            missing.append(f"[{section}] {key}")

    if missing:
        print(f"config.ini has unfilled values:\n  " + "\n  ".join(missing))
        print(f"Edit {config_path} and restart.")
        sys.exit(1)

    return cfg


cfg = load_config()

# ─── Config values ────────────────────────────────────────────────────────────

PLEX_URL        = cfg.get("plex",   "url",          fallback="http://localhost:32400")
PLEX_TOKEN      = cfg.get("plex",   "token")
LOG_PATH        = cfg.get("plex",   "log")
LASTFM_API_KEY      = cfg.get("lastfm", "api_key")
LASTFM_API_SECRET   = cfg.get("lastfm", "api_secret")
LASTFM_USERNAME     = cfg.get("lastfm", "username")
LASTFM_PASSWORD_MD5 = cfg.get("lastfm", "password_md5")
POLL_SECONDS        = cfg.getfloat("scrobbler", "poll_seconds", fallback=0.25)
SCROBBLER_LOG_MAX_MB  = cfg.getint("scrobbler", "log_max_mb",   fallback=5)
SCROBBLER_LOG_BACKUPS = cfg.getint("scrobbler", "log_backups",  fallback=3)

# ─── Logger setup ─────────────────────────────────────────────────────────────

def _running_as_service():
    """True when launched by the Windows SCM (pywin32 present and no console)."""
    try:
        import win32service  # noqa: F401
        return not sys.stdout or sys.stdout.fileno() < 0
    except Exception:
        return False


def setup_logger():
    log_path = Path(__file__).parent / SCROBBLER_LOG_FILE
    logger   = logging.getLogger("PlexScrobbler")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_path,
        maxBytes=SCROBBLER_LOG_MAX_MB * 1024 * 1024,
        backupCount=SCROBBLER_LOG_BACKUPS,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if not _running_as_service():
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    return logger


log = setup_logger()

# ─── Patterns ─────────────────────────────────────────────────────────────────

RE_TIMELINE = re.compile(
    r"\[Req#(?P<req>[^\]]+)\] Client \[(?P<client>[^\]]+)\] reporting timeline state (?P<state>\w+),"
    r" progress of \d+/\d+ms .* ratingKey=(?P<ratingKey>\d+)"
)
RE_SCROBBLE = re.compile(
    r"\[Req#(?P<req>[^\]]+)\] Statistics: \((?P<client>[^\)]+)\) Reporting active playback "
    r"in state \d+ of type \d+ \(scrobble: (?P<scrobble>\d)\)"
)
RE_DEVICE = re.compile(
    r"\[Req#(?P<req>[^\]]+)\] \[Now\] Device is (?P<device>.+)\."
)

# ─── State ────────────────────────────────────────────────────────────────────

sessions      = {}
client_to_req = {}
_stop_flag    = False

# Tracks which (client_uuid, rating_key) pairs have already had now playing sent.
# Cleared when the rating_key changes for a client.
_now_playing_sent = set()   # set of (client, ratingKey) tuples

# ─── Config helpers ───────────────────────────────────────────────────────────

def load_sources():
    config_path = Path(__file__).parent / SOURCES_FILE
    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load {SOURCES_FILE}: {e}. Using defaults.")
    return DEFAULT_SOURCES.copy()


def save_sources(sources):
    config_path = Path(__file__).parent / SOURCES_FILE
    with open(config_path, "w") as f:
        json.dump(sources, f, indent=2)


def source_enabled_for_device(device_str, sources):
    for name, cfg in sources.items():
        if cfg["match"].lower() in device_str.lower():
            return name, cfg["enabled"]
    return None, False


# ─── Plex API ─────────────────────────────────────────────────────────────────

def get_track_metadata(rating_key):
    url = f"{PLEX_URL}/library/metadata/{rating_key}"
    try:
        r = requests.get(url, params={"X-Plex-Token": PLEX_TOKEN}, timeout=5)
        r.raise_for_status()
        root  = ET.fromstring(r.text)
        track = root.find(".//Track")
        if track is None:
            return None
        return {
            "title":        track.get("title", "Unknown Title"),
            "artist":       track.get("grandparentTitle", "Unknown Artist"),
            "album":        track.get("parentTitle", "Unknown Album"),
            "duration":     int(track.get("duration", 0)) // 1000,
            "track_number": track.get("index"),
        }
    except Exception as e:
        log.error(f"Plex metadata lookup failed for ratingKey={rating_key}: {e}")
        return None


# ─── Last.fm ──────────────────────────────────────────────────────────────────

_lastfm_session_key = None


def lastfm_auth():
    global _lastfm_session_key
    if _lastfm_session_key:
        return _lastfm_session_key
    params = {
        "method":    "auth.getMobileSession",
        "username":  LASTFM_USERNAME,
        "authToken": hashlib.md5((LASTFM_USERNAME + LASTFM_PASSWORD_MD5).encode()).hexdigest(),
        "api_key":   LASTFM_API_KEY,
    }
    params["api_sig"] = lastfm_sign(params)
    params["format"]  = "json"
    try:
        r    = requests.post("https://ws.audioscrobbler.com/2.0/", data=params, timeout=10)
        data = r.json()
        _lastfm_session_key = data["session"]["key"]
        log.info(f"Last.fm authenticated as {LASTFM_USERNAME}")
        return _lastfm_session_key
    except Exception as e:
        log.error(f"Last.fm auth failed: {e}")
        return None


def lastfm_sign(params):
    filtered = {k: v for k, v in params.items() if k != "format"}
    sig_str  = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    sig_str  += LASTFM_API_SECRET
    return hashlib.md5(sig_str.encode("utf-8")).hexdigest()


def lastfm_scrobble(track):
    sk = lastfm_auth()
    if not sk:
        log.warning("Skipping scrobble — not authenticated with Last.fm")
        return False
    params = {
        "method":    "track.scrobble",
        "artist":    track["artist"],
        "track":     track["title"],
        "album":     track["album"],
        "timestamp": str(int(time.time())),
        "api_key":   LASTFM_API_KEY,
        "sk":        sk,
    }
    if track.get("track_number"):
        params["trackNumber"] = track["track_number"]
    if track.get("duration"):
        params["duration"] = str(track["duration"])
    params["api_sig"] = lastfm_sign(params)
    params["format"]  = "json"
    try:
        r    = requests.post("https://ws.audioscrobbler.com/2.0/", data=params, timeout=10)
        data = r.json()
        if "scrobbles" in data:
            accepted = data["scrobbles"]["@attr"]["accepted"]
            log.info(f"Scrobbled: {track['artist']} — {track['title']} (accepted={accepted})")
            return True
        else:
            log.error(f"Scrobble error response: {data}")
            return False
    except Exception as e:
        log.error(f"Last.fm scrobble request failed: {e}")
        return False


def lastfm_now_playing(track):
    sk = lastfm_auth()
    if not sk:
        return False
    params = {
        "method":   "track.updateNowPlaying",
        "artist":   track["artist"],
        "track":    track["title"],
        "album":    track["album"],
        "api_key":  LASTFM_API_KEY,
        "sk":       sk,
    }
    if track.get("track_number"):
        params["trackNumber"] = track["track_number"]
    if track.get("duration"):
        params["duration"] = str(track["duration"])
    params["api_sig"] = lastfm_sign(params)
    params["format"]  = "json"
    try:
        r    = requests.post("https://ws.audioscrobbler.com/2.0/", data=params, timeout=10)
        data = r.json()
        if "nowplaying" in data:
            log.info(f"Now playing: {track['artist']} — {track['title']}")
            return True
        else:
            log.error(f"Now playing error response: {data}")
            return False
    except Exception as e:
        log.error(f"Last.fm now playing request failed: {e}")
        return False


# ─── Log tail ─────────────────────────────────────────────────────────────────

def _file_id(path):
    """Return a value that changes when the file is replaced/rotated."""
    try:
        st = os.stat(path)
        # On Windows, st_ino is always 0, so we use (size, ctime) as a proxy.
        # If the file shrinks or its creation time changes, it has been rotated.
        return (st.st_size, st.st_ctime)
    except OSError:
        return None


def tail_log(path):
    """
    Yield new lines from path forever, surviving log rotation.
    Detects rotation by watching for the file to shrink or be recreated,
    then reopens it and continues from the beginning of the new file.
    """
    f           = None
    last_pos    = 0
    last_id     = None
    CHECK_EVERY = 50   # check for rotation every N empty polls

    empty_polls = 0

    try:
        while not _stop_flag:
            # Open or reopen the file
            if f is None:
                try:
                    f        = open(path, "r", encoding="utf-8", errors="replace")
                    f.seek(0, 2)          # start at end on first open
                    last_pos = f.tell()
                    last_id  = _file_id(path)
                    log.info(f"Watching: {path}")
                except OSError as e:
                    log.warning(f"Could not open log file: {e} — retrying in 5s")
                    time.sleep(5)
                    continue

            line = f.readline()
            if line:
                empty_polls = 0
                last_pos    = f.tell()
                yield line.rstrip()
            else:
                empty_polls += 1
                time.sleep(POLL_SECONDS)

                # Periodically check whether the file has been rotated
                if empty_polls >= CHECK_EVERY:
                    empty_polls = 0
                    current_id  = _file_id(path)

                    if current_id is None:
                        # File disappeared — wait for Plex to recreate it
                        log.warning("Log file vanished — waiting for it to reappear")
                        f.close()
                        f = None
                        continue

                    # File rotated if it shrank (new file starts small) or
                    # its identity changed (ctime differs)
                    cur_size = current_id[0]
                    if current_id != last_id and cur_size < last_pos:
                        log.info("Log rotation detected — reopening log file")
                        f.close()
                        f           = None
                        last_pos    = 0
                        last_id     = None
    finally:
        if f:
            f.close()


# ─── Line processor ───────────────────────────────────────────────────────────

def process_line(line, sources):
    global sessions, client_to_req

    m = RE_TIMELINE.search(line)
    if m:
        req, client = m.group("req"), m.group("client")
        new_state   = m.group("state")
        rating_key  = m.group("ratingKey")

        if req not in sessions:
            sessions[req] = {"client": client, "device": None,
                             "ratingKey": rating_key,
                             "state": new_state, "scrobbled": False}
        else:
            prev_rating_key = sessions[req].get("ratingKey")
            sessions[req]["state"]     = new_state
            sessions[req]["ratingKey"] = rating_key

            # Track changed — clear now playing record and scrobble flag
            # so the new track gets fresh calls.
            if rating_key != prev_rating_key:
                _now_playing_sent.discard((client, prev_rating_key))
                sessions[req]["scrobbled"] = False

        client_to_req[client] = req
        return

    m = RE_DEVICE.search(line)
    if m:
        req    = m.group("req")
        device = m.group("device")
        if req in sessions:
            sessions[req]["device"] = device

            # Now we know the device — if the session is already playing
            # and we haven't sent now playing for this client+track yet, do it.
            sess       = sessions[req]
            client     = sess["client"]
            rating_key = sess["ratingKey"]
            np_key     = (client, rating_key)

            if (sess.get("state") == "playing"
                    and np_key not in _now_playing_sent):
                source_name, enabled = source_enabled_for_device(device, sources)
                if source_name:
                    track = get_track_metadata(rating_key)
                    if enabled:
                        if track:
                            lastfm_now_playing(track)
                    else:
                        if track:
                            log.info(f"Now playing on '{source_name}' (scrobbling disabled): {track['artist']} — {track['title']}")
                        else:
                            log.info(f"Now playing on '{source_name}' (scrobbling disabled): ratingKey={rating_key}")
                _now_playing_sent.add(np_key)
        return

    m = RE_SCROBBLE.search(line)
    if m:
        if int(m.group("scrobble")) != 1:
            return
        req  = m.group("req")
        sess = sessions.get(req)
        if not sess or sess.get("scrobbled"):
            return

        device     = sess.get("device") or ""
        rating_key = sess.get("ratingKey")

        source_name, enabled = source_enabled_for_device(device, sources)

        if not source_name:
            log.info(f"Unknown source: '{device}' — not scrobbling")
            sess["scrobbled"] = True
            return

        if not enabled:
            log.info(f"Source '{source_name}' disabled — suppressing scrobble")
            sess["scrobbled"] = True
            return

        track = get_track_metadata(rating_key)
        if track:
            log.info(f"[{source_name}] Scrobbling: {track['artist']} — {track['title']} ({track['album']})")
            lastfm_scrobble(track)
        else:
            log.error(f"[{source_name}] Could not fetch metadata for ratingKey={rating_key}")

        sess["scrobbled"] = True


# ─── Session cleanup ──────────────────────────────────────────────────────────

def cleanup_sessions():
    if len(sessions) > 500:
        recent = list(sessions.keys())[-200:]
        for k in list(sessions.keys()):
            if k not in recent:
                del sessions[k]


# ─── Core run loop ────────────────────────────────────────────────────────────

def run():
    global _stop_flag
    _stop_flag = False

    log.info("=" * 50)
    log.info("Plex Last.fm Scrobbler starting")
    log.info("=" * 50)

    config_path = Path(__file__).parent / SOURCES_FILE
    if not config_path.exists():
        save_sources(DEFAULT_SOURCES)
        log.info(f"Created {SOURCES_FILE} with default sources")

    if not Path(LOG_PATH).exists():
        log.error(f"Log file not found: {LOG_PATH}")
        log.error("Update the [plex] log value in config.ini.")
        return

    lastfm_auth()

    cleanup_counter = 0
    sources_mtime   = 0
    sources         = load_sources()

    for line in tail_log(LOG_PATH):
        try:
            mt = os.path.getmtime(config_path)
            if mt != sources_mtime:
                sources       = load_sources()
                sources_mtime = mt
                log.info(f"Reloaded sources: {list(sources.keys())}")
        except Exception:
            sources = load_sources()

        process_line(line, sources)

        cleanup_counter += 1
        if cleanup_counter % 10000 == 0:
            cleanup_sessions()

    log.info("Scrobbler stopped.")


# ─── Windows Service wrapper ──────────────────────────────────────────────────

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    import threading

    class PlexScrobblerService(win32serviceutil.ServiceFramework):
        _svc_name_         = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_  = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            global _stop_flag
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            _stop_flag = True
            win32event.SetEvent(self.stop_event)
            log.info("Service stop requested.")

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            thread.join(timeout=10)

    _SERVICE_AVAILABLE = True

except ImportError:
    _SERVICE_AVAILABLE = False


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in (
        "install", "remove", "start", "stop", "restart", "update", "debug"
    ):
        if not _SERVICE_AVAILABLE:
            print("ERROR: pywin32 is not installed.")
            print("       Run:  pip install pywin32")
            print("       Then: python plex_scrobbler.py install")
            sys.exit(1)
        win32serviceutil.HandleCommandLine(PlexScrobblerService)
    else:
        run()
