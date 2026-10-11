# MigFin - a calm 10-foot Jellyfin client built on mpv.
# Copyright (C) 2026 MigFin contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The whole application lives in this one file on purpose, so that updating the
# project means replacing a single file. Sections are marked with "# ====".
import ctypes
import io
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import wx

APP_NAME = "MigFin"
BUILD = "10"
VERSION = f"alpha {BUILD}"


# ==== config ==============================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings file, folders, and the default (comfort-first) mpv.conf."""
import json
import os
import uuid
from pathlib import Path



def app_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def mpv_dir() -> Path:
    """MigFin's own mpv config folder: mpv.conf, input.conf, scripts/, script-opts/."""
    d = app_dir() / "mpv"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "script-opts").mkdir(parents=True, exist_ok=True)
    conf = d / "mpv.conf"
    if not conf.exists():
        conf.write_text(default_mpv_conf(), encoding="utf-8")
    return d


# Options MigFin re-applies before every playback while "enforce_safe" is on,
# so a stray line in mpv.conf cannot switch a known trigger back on.
SAFE_LOCKS = {
    "dither-depth": "no",            # no dithering of any kind (spatial or temporal)
    "temporal-dither": "no",         # belt and braces
    "interpolation": "no",           # no frame blending / motion smoothing
    "video-sync": "audio",           # never retime video to the display (display-resample)
    "deband": "no",                  # deband injects per-frame noise
    "target-colorspace-hint": "no",  # never switch the display into HDR
    "hdr-compute-peak": "no",        # static tone mapping, no scene-by-scene brightness shifts
    "d3d11-output-format": "rgba8",  # 8-bit swap chain on Windows
}


def default_mpv_conf() -> str:
    lines = [
        "# MigFin's mpv.conf. This is a normal mpv config file: anything mpv",
        "# understands works here. Your own input.conf and scripts/ go next to it.",
        "#",
        "# The block below is the comfort profile. While \"enforce_safe\" is true in",
        "# config.json, MigFin re-applies these values before each playback.",
        "",
    ]
    lines += [f"{k}={v}" for k, v in SAFE_LOCKS.items() if k != "d3d11-output-format"]
    if os.name == "nt":
        lines += ["gpu-api=d3d11", "d3d11-output-format=rgba8"]
    lines += [
        "",
        "# Decoding. dynamic-crop.lua needs software decoding or a '-copy' mode.",
        "hwdec=auto-copy",
        "",
        "# Window and on-screen controller: appear and disappear instantly, no fades.",
        "fullscreen=yes",
        "script-opts-append=osc-fadeduration=0",
        "",
        "# Optional: uncomment to stop synthesised film grain (AV1 / some H.274 video).",
        "# vd-lavc-film-grain=cpu",
        "",
    ]
    return "\n".join(lines)


class Config:
    DEFAULTS = {
        "server": "",
        "username": "",
        "user_id": "",
        "token": "",
        "device_id": "",
        "enforce_safe": True,      # re-apply the comfort mpv options before every playback
        "text_size": "large",      # large | medium | small
        "artwork": True,           # posters in a Windows icon view; false: a plain text list
        "fullscreen": True,        # false: an ordinary maximised window
    }

    def __init__(self):
        self.path = app_dir() / "config.json"
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if not self.data["device_id"]:
            self.data["device_id"] = uuid.uuid4().hex
            self.save()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


# ==== background work =====================================================
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="migfin")


def to_gui(fn):
    """Schedule fn() on the interface thread. Safe to call from any thread."""
    try:
        wx.CallAfter(fn)
    except Exception:
        pass    # the app is closing


def run_async(fn, callback):
    """Run fn() on a worker thread, then callback(result, error) on the interface thread."""

    def work():
        try:
            result, error = fn(), None
        except Exception as exc:  # reported to the interface, never swallowed silently
            result, error = None, exc
        to_gui(lambda: callback(result, error))

    try:
        _pool.submit(work)
    except RuntimeError:
        pass  # pool already shut down during exit


def shutdown_pool():
    _pool.shutdown(wait=False, cancel_futures=True)


# ==== jellyfin ============================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Jellyfin REST client (standard library only)."""
import json
import urllib.error
import urllib.parse
import urllib.request


FIELDS = "Overview,PrimaryImageAspectRatio"


class JellyfinError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalise_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class Jellyfin:
    def __init__(self, server, device_id, token="", user_id=""):
        self.server = normalise_server(server)
        self.device_id = device_id
        self.token = token
        self.user_id = user_id

    # ---- plumbing -------------------------------------------------------
    def _auth(self) -> str:
        value = (f'MediaBrowser Client="{APP_NAME}", Device="PC", '
                 f'DeviceId="{self.device_id}", Version="{VERSION}"')
        if self.token:
            value += f', Token="{self.token}"'
        return value

    def _request(self, method, path, params=None, body=None, timeout=20):
        url = self.server + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise JellyfinError("The server rejected the sign-in.", 401) from exc
            raise JellyfinError(f"Server error {exc.code} for {path}", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise JellyfinError(f"Cannot reach {self.server}: {getattr(exc, 'reason', exc)}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise JellyfinError("Unexpected reply. Is this a Jellyfin server address?") from exc

    @staticmethod
    def _items(reply):
        if isinstance(reply, dict):
            return reply.get("Items") or []
        return reply or []

    # ---- session --------------------------------------------------------
    def login(self, username, password):
        reply = self._request("POST", "/Users/AuthenticateByName",
                              body={"Username": username, "Pw": password})
        if not reply or "AccessToken" not in reply:
            raise JellyfinError("Sign-in failed.")
        self.token = reply["AccessToken"]
        self.user_id = reply["User"]["Id"]
        return reply

    # ---- browsing -------------------------------------------------------
    def views(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Views"))

    def resume(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items/Resume", {
            "Limit": 40, "MediaTypes": "Video", "Fields": FIELDS}))

    def next_up(self):
        return self._items(self._request("GET", "/Shows/NextUp", {
            "UserId": self.user_id, "Limit": 40, "Fields": FIELDS}))

    def latest(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items/Latest", {
            "Limit": 60, "Fields": FIELDS}))

    def children(self, item):
        kind = item.get("Type")
        if kind == "Series":
            return self._items(self._request("GET", f"/Shows/{item['Id']}/Seasons", {
                "UserId": self.user_id, "Fields": FIELDS}))
        if kind == "Season" and item.get("SeriesId"):
            return self._items(self._request("GET", f"/Shows/{item['SeriesId']}/Episodes", {
                "UserId": self.user_id, "SeasonId": item["Id"], "Fields": FIELDS}))
        params = {"ParentId": item["Id"], "Fields": FIELDS, "SortBy": "SortName", "SortOrder": "Ascending"}
        collection = (item.get("CollectionType") or "").lower()
        if collection == "movies":
            params.update(Recursive="true", IncludeItemTypes="Movie")
        elif collection == "tvshows":
            params.update(Recursive="true", IncludeItemTypes="Series")
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items", params))

    # ---- media ----------------------------------------------------------
    def image_url(self, item, wide, width, height):
        """URL for a poster (or a 16:9 still when wide). None if the item has no art."""
        tags = item.get("ImageTags") or {}
        item_id, kind = item["Id"], "Primary"
        if wide and item.get("Type") != "Episode":
            if item.get("BackdropImageTags"):
                kind = "Backdrop"
            elif "Thumb" in tags:
                kind = "Thumb"
        if kind == "Primary" and "Primary" not in tags:
            if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
                item_id = item["SeriesId"]
            else:
                return None
        query = urllib.parse.urlencode({"maxWidth": width, "maxHeight": height, "quality": 90})
        return f"{self.server}/Items/{item_id}/Images/{kind}?{query}"

    def fetch(self, url, timeout=20) -> bytes:
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def stream_url(self, item) -> str:
        """Direct play: mpv gets the original file, untouched by the server."""
        query = urllib.parse.urlencode({"static": "true", "mediaSourceId": item["Id"],
                                        "api_key": self.token})
        return f"{self.server}/Videos/{item['Id']}/stream?{query}"

    # ---- playback reporting --------------------------------------------
    def report(self, event, item_id, seconds, paused=False, timeout=20):
        """event: 'start' | 'progress' | 'stop'. Keeps resume points and watched state in sync."""
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                "stop": "/Sessions/Playing/Stopped"}[event]
        self._request("POST", path, body={
            "ItemId": item_id, "MediaSourceId": item_id,
            "PositionTicks": int(max(seconds or 0, 0) * 10_000_000),
            "IsPaused": bool(paused), "CanSeek": True, "PlayMethod": "DirectPlay"}, timeout=timeout)


# ==== player ==============================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""mpv playback through libmpv.

mpv opens its own fullscreen window and handles its own input, so every mpv
shortcut, input.conf binding, script and OSC behaves exactly as in stock mpv.
The one deliberate change: q / Q / closing the window stop playback and return
to the library instead of quitting the whole player core.
"""
import locale
import os
import sys
import time



def _prepare_dll_search():
    """Let python-mpv find libmpv-2.dll next to the exe or the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [here, os.path.dirname(here), os.getcwd()]
    if getattr(sys, "frozen", False):
        dirs = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")] + dirs
    for d in dirs:
        if d and os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass


class Player:
    def __init__(self, enforce_safe=True, on_finished=None):
        self.enforce_safe = enforce_safe
        self.on_finished = on_finished      # called on the GUI thread: fn(position_or_None)
        self._mpv = None
        self.active = False                 # a file is loading or playing
        self.pos = None                     # last known position in seconds
        self.paused = False
        self._started = False               # mpv has left idle for the current file
        self._play_time = 0.0

    # ---- lifecycle ------------------------------------------------------
    def _ensure(self):
        if self._mpv is not None:
            return self._mpv
        _prepare_dll_search()
        locale.setlocale(locale.LC_NUMERIC, "C")   # libmpv requirement
        import mpv  # imported late so a missing DLL becomes a readable error

        m = mpv.MPV(config="yes", config_dir=str(mpv_dir()),
                    input_default_bindings="yes", input_vo_keyboard="yes",
                    osc="yes", ytdl="no", idle="yes")
        for key in ("q", "Q", "CLOSE_WIN", "STOP"):
            try:
                m.command("keybind", key, "stop")
            except Exception:
                pass
        m.observe_property("time-pos", self._on_pos)
        m.observe_property("pause", self._on_pause)
        m.observe_property("idle-active", self._on_idle)
        self._mpv = m
        return m

    def _apply_locks(self, m):
        if not self.enforce_safe:
            return
        for name, value in SAFE_LOCKS.items():
            try:
                m[name] = value
            except Exception:
                pass  # option not present in this mpv build / platform

    def shutdown(self):
        m, self._mpv = self._mpv, None
        self.active = False
        if m is not None:
            try:
                m.terminate()
            except Exception:
                pass

    # ---- control --------------------------------------------------------
    def play(self, url, title, start=0.0):
        last_error = None
        for _ in range(2):
            try:
                m = self._ensure()
                self._apply_locks(m)
                m["force-media-title"] = title
                m["start"] = f"{start:.1f}" if start and start > 0 else "none"
                m["pause"] = "no"
                self.pos, self.paused, self._started = None, False, False
                self._play_time = time.monotonic()
                self.active = True
                m.command("loadfile", url, "replace")
                return
            except OSError:
                self.active = False
                raise   # libmpv itself could not be loaded; a retry will not help
            except Exception as exc:
                # The core may have been shut down (e.g. a custom 'quit' binding). Rebuild once.
                last_error = exc
                self.active = False
                self._mpv = None
        raise last_error

    def command(self, *args):
        if self._mpv is not None:
            try:
                self._mpv.command(*args)
            except Exception:
                pass

    def alive(self) -> bool:
        """False if the mpv core died while we believed something was playing."""
        if self._mpv is None:
            return False
        try:
            self._mpv["pause"]
            return True
        except Exception:
            return False

    def abandon(self):
        """Forget a dead core; a fresh one is created on the next play()."""
        self._mpv = None
        self.active = False

    # ---- mpv event thread callbacks ------------------------------------
    def _on_pos(self, _name, value):
        if value is not None and self.active:
            self.pos = value

    def _on_pause(self, _name, value):
        self.paused = bool(value)

    def stalled(self) -> bool:
        """True if a file was requested but mpv never began loading it."""
        return self.active and not self._started and time.monotonic() - self._play_time > 30

    def _on_idle(self, _name, value):
        if not value:
            if self.active:
                self._started = True
            return
        # The initial idle=yes notification can arrive after play(); ignore it.
        if self.active and self._started:
            self.active = False
            pos = self.pos
            if self.on_finished:
                to_gui(lambda: self.on_finished(pos))


# ==== gamepad =============================================================
"""Optional XInput controller support (Windows). Does nothing elsewhere."""

_PAD_BUTTONS = {0x0001: "UP", 0x0002: "DOWN", 0x0004: "LEFT", 0x0008: "RIGHT",
                0x0010: "START", 0x0020: "BACK",
                0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y"}
_PAD_REPEATING = {"UP", "DOWN", "LEFT", "RIGHT"}
_PAD_STICK = 16000


class _Pad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short),
                ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _PadState(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("pad", _Pad)]


class Gamepad:
    def __init__(self):
        self._dll = None
        self._index = None
        self._ticks = 0
        self._held = {}
        if os.name == "nt":
            for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
                try:
                    self._dll = ctypes.WinDLL(name)
                    break
                except OSError:
                    continue
        self.available = self._dll is not None

    def _read(self):
        state = _PadState()
        if self._index is None:
            self._ticks += 1
            if self._ticks % 50 != 1:       # look for a controller every ~2 s
                return None
            for i in range(4):
                if self._dll.XInputGetState(i, ctypes.byref(state)) == 0:
                    self._index = i
                    return state.pad
            return None
        if self._dll.XInputGetState(self._index, ctypes.byref(state)) != 0:
            self._index = None
            return None
        return state.pad

    def poll(self):
        """Call every ~40 ms. Returns the buttons that count as pressed right now."""
        if not self.available:
            return []
        pad = self._read()
        down = set()
        if pad is not None:
            down = {name for bit, name in _PAD_BUTTONS.items() if pad.buttons & bit}
            if pad.lx < -_PAD_STICK: down.add("LEFT")
            if pad.lx > _PAD_STICK: down.add("RIGHT")
            if pad.ly > _PAD_STICK: down.add("UP")
            if pad.ly < -_PAD_STICK: down.add("DOWN")
        now = time.monotonic()
        for name in list(self._held):
            if name not in down:
                del self._held[name]
        pressed = []
        for name in down:
            due = self._held.get(name)
            if due is None:
                self._held[name] = now + 0.45 if name in _PAD_REPEATING else float("inf")
                pressed.append(name)
            elif now >= due:
                self._held[name] = now + 0.14
                pressed.append(name)
        return pressed


# ==== interface ==========================================================
# A Kodi-style layout drawn with plain Windows GDI calls: a home screen with a
# horizontal menu band, then a library screen of posters or text rows.
# Navy blue throughout, dark red for the highlight. Nothing animates.

NAVY = (10, 24, 61)          # background
BAND = (6, 15, 40)           # menu band, poster placeholders under the highlight
PANEL = (24, 46, 104)        # fields, keys, placeholders, unfocused selection
RED = (139, 26, 34)          # highlight
RED_LINE = (170, 34, 44)     # rules on the band, underline of the current menu word
TEXT = (232, 236, 245)
MUTED = (138, 152, 190)

TEXT_SIZES = {"large": 20, "medium": 14, "small": 10}
HOME_SCALE = {"large": 1.0, "medium": 0.8, "small": 0.65}
TEXT_SIZE_ORDER = ["large", "medium", "small"]
POSTER_SIZE = (150, 225)      # at 100% display scaling; Windows scaling is applied on top
WIDE_SIZE = (256, 144)        # episode stills
HOME_TEXT_HEIGHT = 0.064      # menu word height as a share of screen height (Kodi's, less 25%)
ENTER_KEYS = (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
BACK_KEYS = (wx.WXK_ESCAPE, wx.WXK_BACK)

OSK_LAYERS = {
    "lower": ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm.:/"],
    "upper": ["1234567890", "QWERTYUIOP", "ASDFGHJKL@", "ZXCVBNM.:/"],
    "symbols": ['!"£$%^&*()', "-_=+[]{};:", "'@#~,.<>/?", "\\|`€¥§°¬¦·"],
}
# (label, first column, width in columns) - widths add up to 10
OSK_SPECIAL = [("Shift", 0, 2), ("#+=", 2, 1), ("-", 3, 1), ("_", 4, 1),
               ("Space", 5, 2), ("Delete", 7, 1), ("Done", 8, 2)]
OSK_ROWS, OSK_COLS = 4, 10

MPV_KEYS = {wx.WXK_LEFT: "LEFT", wx.WXK_RIGHT: "RIGHT", wx.WXK_UP: "UP", wx.WXK_DOWN: "DOWN",
            wx.WXK_RETURN: "ENTER", wx.WXK_NUMPAD_ENTER: "ENTER", wx.WXK_ESCAPE: "ESC",
            wx.WXK_SPACE: "SPACE", wx.WXK_BACK: "BS", wx.WXK_TAB: "TAB",
            wx.WXK_PAGEUP: "PGUP", wx.WXK_PAGEDOWN: "PGDWN", wx.WXK_HOME: "HOME",
            wx.WXK_END: "END", wx.WXK_DELETE: "DEL"}


def colour(rgb):
    return wx.Colour(*rgb)


def clock(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def item_label(item: dict) -> str:
    name = item.get("Name") or "Untitled"
    if item.get("Type") == "Episode":
        season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if season is not None and number is not None:
            return f"S{season}:E{number}  {name}"
        return name
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year and item.get("Type") == "Movie" else name


def row_label(item: dict) -> str:
    """One entry of the library: title plus watched / resume state."""
    label = item_label(item)
    user = item.get("UserData") or {}
    if user.get("PlaybackPositionTicks"):
        label += f"  (resume at {clock(user['PlaybackPositionTicks'] / 10_000_000)})"
    elif user.get("Played"):
        label += "  (watched)"
    elif user.get("UnplayedItemCount"):
        label += f"  ({user['UnplayedItemCount']} unwatched)"
    return label


def wrap_two_lines(dc, text, width):
    """Split a label over at most two lines that fit the width; the second is cut with '...'."""
    if dc.GetTextExtent(text)[0] <= width:
        return [text]
    words, first = text.split(" "), ""
    while words:
        trial = (first + " " + words[0]).strip()
        if dc.GetTextExtent(trial)[0] > width:
            break
        first = trial
        words.pop(0)
    if not first:                      # one very long word
        return [wx.Control.Ellipsize(text, dc, wx.ELLIPSIZE_END, width, wx.ELLIPSIZE_FLAGS_NONE)]
    rest = wx.Control.Ellipsize(" ".join(words), dc, wx.ELLIPSIZE_END, width, wx.ELLIPSIZE_FLAGS_NONE)
    return [first, rest]


class QuietText(wx.TextCtrl):
    """Read-only text that wraps by itself and never takes keyboard focus."""

    def AcceptsFocus(self):
        return False

    def AcceptsFocusFromKeyboard(self):
        return False


class HomePanel(wx.Panel):
    """The home screen: a dark band low on the screen carrying the menu words."""

    def __init__(self, parent, on_open):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.titles = []
        self.sel = 0
        self.scale = 1.0
        self.on_open = on_open
        self.hits = []                 # (x1, x2, index) of the words as last drawn
        self.band = (0, 0)             # (y, height) of the band as last drawn
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)

    def AcceptsFocus(self):
        return True

    def set_titles(self, titles):
        self.titles = list(titles)
        self.sel = 0
        self.Refresh()

    def move(self, delta):
        target = max(0, min(len(self.titles) - 1, self.sel + delta))
        if target != self.sel:
            self.sel = target
            self.Refresh()

    def _on_click(self, event):
        x, y = event.GetPosition()
        if self.band[0] <= y <= self.band[0] + self.band[1]:
            for x1, x2, index in self.hits:
                if x1 <= x <= x2:
                    self.sel = index
                    self.Refresh()
                    self.on_open()
                    break
        self.SetFocus()

    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        size = self.GetClientSize()
        w, h = size.width, size.height
        dc.SetBackground(wx.Brush(colour(NAVY)))
        dc.Clear()
        dc.SetPen(wx.TRANSPARENT_PEN)

        px = max(16, int(h * HOME_TEXT_HEIGHT * self.scale))
        small = wx.Font(wx.FontInfo(wx.Size(0, max(12, int(px * 0.34)))).FaceName("Segoe UI"))
        dc.SetFont(wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName("Bahnschrift SemiBold Condensed")))
        labels = [title.upper() for title in self.titles]
        widths = [dc.GetTextExtent(label)[0] for label in labels]
        text_h = dc.GetTextExtent("M")[1]
        gap = int(px * 1.4)
        band_h = int(text_h * 1.55)
        band_y = int(h * 0.72)
        self.band = (band_y, band_h)

        dc.SetBrush(wx.Brush(colour(BAND)))
        dc.DrawRectangle(0, band_y, w, band_h)
        dc.SetBrush(wx.Brush(colour(RED_LINE)))
        dc.DrawRectangle(0, band_y, w, 3)
        dc.DrawRectangle(0, band_y + band_h - 3, w, 3)

        # The words stay put and only the highlight moves. They shift sideways only
        # when there are too many to fit, just enough to keep the current one centred.
        total = sum(widths) + gap * max(len(labels) - 1, 0)
        starts, x = [], 0
        for width in widths:
            starts.append(x)
            x += width + gap
        if total <= w - 2 * gap or not labels:
            origin = (w - total) // 2
        else:
            origin = w // 2 - (starts[self.sel] + widths[self.sel] // 2)
            origin = max(w - gap - total, min(gap, origin))
        self.hits = []
        text_y = band_y + (band_h - text_h) // 2
        for index, label in enumerate(labels):
            left = origin + starts[index]
            current = index == self.sel
            dc.SetTextForeground(colour(TEXT if current else MUTED))
            dc.DrawText(label, left, text_y)
            if current:
                dc.SetBrush(wx.Brush(colour(RED_LINE)))
                dc.DrawRectangle(left, band_y + band_h - 3 - max(5, px // 9), widths[index], max(5, px // 9))
            self.hits.append((left, left + widths[index], index))

        dc.SetFont(small)
        dc.SetTextForeground(colour(MUTED))
        stamp = time.strftime("%A, %d %B %Y  |  %H:%M").upper()
        stamp_w, stamp_h = dc.GetTextExtent(stamp)
        dc.DrawText(stamp, w - stamp_w - gap // 2, band_y - stamp_h - 8)
        dc.DrawText(VERSION, gap // 2, h - stamp_h - 12)
        dc.SetTextForeground(colour(TEXT))
        dc.DrawText(APP_NAME.upper(), gap // 2, gap // 3)


class TileView(wx.Panel):
    """A grid of posters or a list of text rows, with a dark red block on the current entry.

    Scrolling is by whole rows and happens in a single step.
    """

    def __init__(self, parent, on_select=None, on_activate=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.labels = []
        self.images = {}               # entry index -> wx.Bitmap
        self.grid = False
        self.thumb = (0, 0)
        self.sel = -1
        self.top = 0                   # first visible row
        self.on_select = on_select
        self.on_activate = on_activate
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_SET_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_KILL_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_double)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)

    def AcceptsFocus(self):
        return True

    # ---- contents -------------------------------------------------------
    def set_rows(self, labels, grid=False, thumb=(0, 0)):
        self.labels = list(labels)
        self.images = {}
        self.grid = grid
        self.thumb = thumb
        self.sel = 0 if self.labels else -1
        self.top = 0
        self.Refresh()

    def set_image(self, index, bitmap):
        self.images[index] = bitmap
        first, last = self.visible_range()
        if first <= index < last:
            self.Refresh()

    # ---- geometry -------------------------------------------------------
    def _client(self):
        size = self.GetClientSize()
        return size.width, size.height

    def metrics(self):
        """(columns, cell width, cell height, left margin, whole rows that fit)"""
        w, h = self._client()
        line = self.GetCharHeight()
        if self.grid:
            pad = max(6, line // 3)
            cell_w = self.thumb[0] + pad * 2
            cell_h = self.thumb[1] + pad * 2 + int(line * 2.3)
            cols = max(1, w // max(cell_w, 1))
            left = (w - cols * cell_w) // 2
        else:
            cell_w, cell_h, cols, left = w, int(line * 1.7), 1, 0
        return cols, cell_w, cell_h, left, max(1, h // max(cell_h, 1))

    def visible_range(self):
        cols, _cw, _ch, _left, rows = self.metrics()
        return self.top * cols, min(len(self.labels), (self.top + rows + 1) * cols)

    # ---- selection ------------------------------------------------------
    def select(self, index, notify=True):
        if not self.labels:
            return
        index = max(0, min(len(self.labels) - 1, index))
        self.sel = index
        cols, _cw, _ch, _left, rows = self.metrics()
        row = index // cols
        if row < self.top:
            self.top = row
        elif row >= self.top + rows:
            self.top = row - rows + 1
        self.Refresh()
        if notify and self.on_select:
            self.on_select()

    def move(self, dx, dy):
        """Move the highlight one step. Returns False at an edge."""
        count = len(self.labels)
        if not count or self.sel < 0:
            return False
        cols = self.metrics()[0]
        if dx:
            target = self.sel + dx
            if target < 0 or target >= count or target // cols != self.sel // cols:
                return False
        else:
            target = self.sel + dy * cols
            if target < 0:
                return False
            if target >= count:
                if self.sel // cols == (count - 1) // cols:
                    return False
                target = count - 1         # short last row: land on its final entry
        self.select(target)
        return True

    def page(self, direction):
        cols, _cw, _ch, _left, rows = self.metrics()
        if self.labels:
            self.select(self.sel + direction * cols * rows)

    def jump(self, char):
        """Go to the next entry starting with this letter or digit."""
        count = len(self.labels)
        for step in range(1, count + 1):
            index = (max(self.sel, 0) + step) % count
            if self.labels[index].lower().startswith(char.lower()):
                self.select(index)
                return True
        return False

    # ---- mouse ----------------------------------------------------------
    def _hit(self, position):
        cols, cell_w, cell_h, left, _rows = self.metrics()
        col = (position.x - left) // max(cell_w, 1)
        index = (position.y // max(cell_h, 1) + self.top) * cols + col
        return index if 0 <= col < cols and 0 <= index < len(self.labels) else -1

    def _on_click(self, event):
        self.SetFocus()
        index = self._hit(event.GetPosition())
        if index >= 0:
            self.select(index)

    def _on_double(self, event):
        if self._hit(event.GetPosition()) >= 0 and self.on_activate:
            self.on_activate()

    def _on_wheel(self, event):
        cols, _cw, _ch, _left, rows = self.metrics()
        last_top = max(0, (len(self.labels) - 1) // cols - rows + 1)
        top = max(0, min(last_top, self.top + (-1 if event.GetWheelRotation() > 0 else 1)))
        if top != self.top:
            self.top = top
            self.Refresh()

    # ---- drawing --------------------------------------------------------
    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(colour(NAVY)))
        dc.Clear()
        dc.SetFont(self.GetFont())
        dc.SetPen(wx.TRANSPARENT_PEN)
        cols, cell_w, cell_h, left, rows = self.metrics()
        line = self.GetCharHeight()
        focused = self.HasFocus()
        pad = max(6, line // 3)
        first = self.top * cols
        for index in range(first, min(len(self.labels), (self.top + rows + 1) * cols)):
            x = left + (index % cols) * cell_w
            y = (index // cols - self.top) * cell_h
            current = index == self.sel
            if current:
                dc.SetBrush(wx.Brush(colour(RED if focused else PANEL)))
                dc.DrawRectangle(x, y, cell_w, cell_h)
            dc.SetTextForeground(colour(TEXT))
            label = self.labels[index]
            if not self.grid:
                text = wx.Control.Ellipsize(label, dc, wx.ELLIPSIZE_END, cell_w - line * 2, wx.ELLIPSIZE_FLAGS_NONE)
                dc.DrawText(text, x + line, y + (cell_h - line) // 2)
                continue
            thumb_w, thumb_h = self.thumb
            bitmap = self.images.get(index)
            if bitmap is not None:
                dc.DrawBitmap(bitmap, x + pad + (thumb_w - bitmap.GetWidth()) // 2,
                              y + pad + (thumb_h - bitmap.GetHeight()) // 2)
            else:
                dc.SetBrush(wx.Brush(colour(BAND if current else PANEL)))
                dc.DrawRectangle(x + pad, y + pad, thumb_w, thumb_h)
            for number, text in enumerate(wrap_two_lines(dc, label, thumb_w)):
                text_w = dc.GetTextExtent(text)[0]
                dc.DrawText(text, x + (cell_w - text_w) // 2, y + pad + thumb_h + 4 + number * line)


class MainFrame(wx.Frame):
    def __init__(self, config):
        super().__init__(None, title=APP_NAME, size=(1600, 900))
        self.config = config
        self.api = None
        self.entries = []            # home menu entries
        self.items = []              # Jellyfin items behind the entries of the library screen
        self.stack = []              # navigation trail inside a library
        self.generation = 0          # bumps on every load; stale replies are dropped
        self.restore_row = 0
        self.wide = False
        self.thumb = (0, 0)
        self.art_generation = 0
        self.art_requested = set()
        self.now_playing = None
        self.ticks = 0
        self.choice_callback = None
        self.osk_target = None
        self.osk_layer = "lower"
        self.page = None
        self.clock_minute = ""
        self.player = Player(enforce_safe=bool(config["enforce_safe"]),
                             on_finished=self._playback_finished)

        self._build_login()
        self.p_home = HomePanel(self, self._home_open)
        self._build_browse()
        self._build_choice()
        self._build_playing()
        self.pages = [self.p_login, self.p_home, self.p_browse, self.p_choice, self.p_playing]
        sizer = wx.BoxSizer(wx.VERTICAL)
        for page in self.pages:
            sizer.Add(page, 1, wx.EXPAND)
            page.Hide()
        self.SetSizer(sizer)

        self.play_timer = wx.Timer(self)
        self.pad_timer = wx.Timer(self)
        self.art_timer = wx.Timer(self)
        self.clock_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._playback_tick(), self.play_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._poll_gamepad(), self.pad_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._request_art(), self.art_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._clock_tick(), self.clock_timer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.gamepad = Gamepad()
        if self.gamepad.available:
            self.pad_timer.Start(40)
        self.art_timer.Start(350)
        self.clock_timer.Start(10000)
        self._apply_look()

    def start(self):
        if self.config["token"] and self.config["server"]:
            self.api = Jellyfin(self.config["server"], self.config["device_id"],
                                self.config["token"], self.config["user_id"])
            self._start_session()
        else:
            self._show_login()

    # ---- construction ---------------------------------------------------
    @staticmethod
    def _centred(panel, column, top=1, bottom=2):
        across = wx.BoxSizer(wx.HORIZONTAL)
        across.AddStretchSpacer(1)
        across.Add(column, 0)
        across.AddStretchSpacer(1)
        down = wx.BoxSizer(wx.VERTICAL)
        down.AddStretchSpacer(top)
        down.Add(across, 0, wx.EXPAND)
        down.AddStretchSpacer(bottom)
        panel.SetSizer(down)

    def _make_button(self, parent, label):
        """A flat navy button that turns dark red while it has the focus."""
        button = wx.Button(parent, label=label, style=wx.BORDER_NONE)
        button.Bind(wx.EVT_SET_FOCUS, lambda e, b=button: self._button_focus(e, b, True))
        button.Bind(wx.EVT_KILL_FOCUS, lambda e, b=button: self._button_focus(e, b, False))
        return button

    @staticmethod
    def _button_focus(event, button, focused):
        button.SetBackgroundColour(colour(RED if focused else PANEL))
        button.Refresh()
        event.Skip()

    def _build_login(self):
        p = self.p_login = wx.Panel(self)
        self.login_title = wx.StaticText(p, label=APP_NAME.upper())
        self.login_hint_text = ("Sign in to your Jellyfin server. Press Enter (A on a controller) on a box "
                                "to type with the on-screen keyboard, or just type on a real keyboard.")
        self.login_hint = wx.StaticText(p, label=self.login_hint_text)
        self.f_server = wx.TextCtrl(p, style=wx.BORDER_SIMPLE)
        self.f_user = wx.TextCtrl(p, style=wx.BORDER_SIMPLE)
        self.f_pass = wx.TextCtrl(p, style=wx.TE_PASSWORD | wx.BORDER_SIMPLE)
        self.fields = [self.f_server, self.f_user, self.f_pass]
        self.b_connect = self._make_button(p, "Connect")
        self.b_connect.Bind(wx.EVT_BUTTON, lambda _e: self._submit_login())
        self.login_error = wx.StaticText(p, label="")
        self.captions = []

        grid = wx.GridBagSizer(4, 4)
        self.key_buttons = []        # 4 rows of 10 character keys
        self.special_buttons = []
        self.key_pos = {}            # button id -> (row, index)
        for r in range(OSK_ROWS):
            line = []
            for c in range(OSK_COLS):
                button = self._make_button(p, "")
                button.Bind(wx.EVT_BUTTON, lambda _e, r=r, c=c: self._osk_press(r, c))
                grid.Add(button, pos=(r, c), flag=wx.EXPAND)
                self.key_pos[button.GetId()] = (r, c)
                line.append(button)
            self.key_buttons.append(line)
        for index, (label, first, width) in enumerate(OSK_SPECIAL):
            button = self._make_button(p, label)
            button.Bind(wx.EVT_BUTTON, lambda _e, i=index: self._osk_press(OSK_ROWS, i))
            grid.Add(button, pos=(OSK_ROWS, first), span=(1, width), flag=wx.EXPAND)
            self.key_pos[button.GetId()] = (OSK_ROWS, index)
            self.special_buttons.append(button)
        self._osk_relabel()

        col = self.login_col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.login_title, 0, wx.BOTTOM, 8)
        col.Add(self.login_hint, 0, wx.BOTTOM, 16)
        for caption, field in (("Server address, for example http://192.168.1.10:8096", self.f_server),
                               ("Username", self.f_user), ("Password", self.f_pass)):
            text = wx.StaticText(p, label=caption)
            self.captions.append(text)
            col.Add(text, 0, wx.BOTTOM, 2)
            col.Add(field, 0, wx.EXPAND | wx.BOTTOM, 10)
        col.Add(self.b_connect, 0, wx.BOTTOM, 8)
        col.Add(self.login_error, 0, wx.EXPAND | wx.BOTTOM, 14)
        col.Add(grid, 0, wx.EXPAND)
        self._centred(p, col, 1, 1)

    def _build_browse(self):
        p = self.p_browse = wx.Panel(self)
        self.crumb = wx.StaticText(p, label="")
        self.status = wx.StaticText(p, label="")
        self.view = TileView(p, on_select=self._show_info, on_activate=self._activate)
        self.info = QuietText(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        self.rule = wx.Panel(p, size=(-1, 3))
        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(self.crumb, 1, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.status, 0, wx.ALIGN_CENTER_VERTICAL)
        page = wx.BoxSizer(wx.VERTICAL)
        page.Add(header, 0, wx.EXPAND | wx.BOTTOM, 8)
        page.Add(self.rule, 0, wx.EXPAND | wx.BOTTOM, 12)
        page.Add(self.view, 1, wx.EXPAND | wx.BOTTOM, 10)
        page.Add(self.info, 0, wx.EXPAND)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(page, 1, wx.EXPAND | wx.ALL, 24)
        p.SetSizer(outer)

    def _build_choice(self):
        p = self.p_choice = wx.Panel(self)
        self.choice_title = wx.StaticText(p, label="")
        self.choice_detail = wx.StaticText(p, label="")
        self.options = TileView(p, on_activate=lambda: self._choice_finish(self.options.sel))
        col = self.choice_col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.choice_title, 0, wx.BOTTOM, 12)
        col.Add(self.choice_detail, 0, wx.EXPAND | wx.BOTTOM, 12)
        col.Add(self.options, 0, wx.EXPAND)
        self._centred(p, col)

    def _build_playing(self):
        p = self.p_playing = wx.Panel(self)
        self.playing_text = wx.StaticText(p, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.playing_text, 0, wx.EXPAND)
        self._centred(p, col)

    def _apply_look(self):
        """Fonts, sizes and the navy / dark red colours for every control."""
        size_name = self.config["text_size"] if self.config["text_size"] in TEXT_SIZES else "large"
        points = TEXT_SIZES[size_name]
        base = wx.Font(wx.FontInfo(points).FaceName("Segoe UI"))
        big = wx.Font(wx.FontInfo(round(points * 1.4)).FaceName("Segoe UI").Bold())

        def walk(window):
            window.SetFont(base)
            window.SetBackgroundColour(colour(NAVY))
            window.SetForegroundColour(colour(TEXT))
            for child in window.GetChildren():
                walk(child)

        walk(self)
        for title in (self.login_title, self.crumb, self.choice_title):
            title.SetFont(big)
        for quiet in [self.status, self.login_hint, self.choice_detail] + self.captions:
            quiet.SetForegroundColour(colour(MUTED))
        for field in self.fields:
            field.SetBackgroundColour(colour(PANEL))
        self.rule.SetBackgroundColour(colour(RED_LINE))
        keys = [b for line in self.key_buttons for b in line] + self.special_buttons
        for button in keys + [self.b_connect]:
            button.SetBackgroundColour(colour(PANEL))
            button.SetForegroundColour(colour(TEXT))
        self.p_home.scale = HOME_SCALE[size_name]

        dc = wx.ClientDC(self)
        dc.SetFont(base)
        char_w, char_h = dc.GetTextExtent("M")
        key = (int(char_h * 2.2), int(char_h * 1.9))
        for button in keys:
            button.SetMinSize(key)
        self.b_connect.SetMinSize((key[0] * 4, key[1]))
        column = key[0] * OSK_COLS + 4 * (OSK_COLS - 1)
        self.login_col.SetMinSize((column, -1))
        self.login_hint.SetLabel(self.login_hint_text)
        self.login_hint.Wrap(column)
        self.info.SetMinSize((-1, int(char_h * 4.4)))
        self.choice_col.SetMinSize((char_w * 34, -1))
        self.options.SetMinSize((-1, int(char_h * 1.7) * 6))
        self.choice_detail.Wrap(char_w * 34)
        for page in self.pages:
            page.Layout()
            page.Refresh()
        self.Layout()

    def _show_page(self, page):
        self.Freeze()
        for other in self.pages:
            other.Show(other is page)
        self.page = page
        self.Layout()
        page.Layout()
        self.Thaw()

    # ---- sign-in --------------------------------------------------------
    def _show_login(self, message=""):
        self.f_server.SetValue(self.config["server"])
        self.f_user.SetValue(self.config["username"])
        self.f_pass.SetValue("")
        self.login_error.SetLabel(message)
        self.b_connect.Enable(True)
        self.osk_target = None
        self._show_page(self.p_login)
        (self.f_pass if self.config["server"] and self.config["username"] else self.f_server).SetFocus()

    def _submit_login(self):
        server, username, password = self.f_server.GetValue(), self.f_user.GetValue().strip(), self.f_pass.GetValue()
        if not server.strip() or not username:
            self.login_error.SetLabel("Enter the server address and your username.")
            return
        api = Jellyfin(server, self.config["device_id"])
        if not password and self.config["token"] and api.server == self.config["server"]:
            # Retry with the saved sign-in (for example the server was unreachable a moment ago).
            self.api = Jellyfin(api.server, self.config["device_id"], self.config["token"], self.config["user_id"])
            self._start_session()
            return
        self.b_connect.Enable(False)
        self.login_error.SetLabel("Connecting")

        def done(_reply, error):
            self.b_connect.Enable(True)
            if error:
                self.login_error.SetLabel(str(error))
                return
            self.api = api
            self.config["server"], self.config["username"] = api.server, username
            self.config["token"], self.config["user_id"] = api.token, api.user_id
            self.config.save()
            self._start_session()

        run_async(lambda: api.login(username, password), done)

    def _start_session(self):
        self._set_home([])
        self._show_page(self.p_home)
        self.p_home.SetFocus()
        api = self.api

        def done(views, error):
            if api is not self.api:
                return
            if error:
                if getattr(error, "status", None) == 401:
                    self.config["token"] = ""
                    self.config.save()
                    self._show_login("Your saved sign-in has expired. Please sign in again.")
                else:
                    self._show_login(f"{error}  Leave the password empty and press Connect to retry.")
                return
            self._set_home(views or [])

        run_async(api.views, done)

    # ---- on-screen keyboard ---------------------------------------------
    def _osk_relabel(self):
        for r, text in enumerate(OSK_LAYERS[self.osk_layer]):
            for c, char in enumerate(text):
                # '&' would otherwise be read as a keyboard-shortcut marker
                self.key_buttons[r][c].SetLabel("&&" if char == "&" else char)
        self.special_buttons[0].SetLabel("SHIFT" if self.osk_layer == "upper" else "Shift")
        self.special_buttons[1].SetLabel("abc" if self.osk_layer == "symbols" else "#+=")

    def _osk_open(self, field):
        self.osk_target = field
        self.key_buttons[1][0].SetFocus()

    def _osk_close(self):
        field, self.osk_target = self.osk_target, None
        order = self.fields + [self.b_connect]
        index = self.fields.index(field) if field in self.fields else -1
        order[index + 1].SetFocus()      # the next box, or Connect after the password

    def _osk_type(self, text):
        field = self.osk_target or self.f_server
        field.SetValue(field.GetValue() + text)
        field.SetInsertionPointEnd()

    def _osk_erase(self):
        field = self.osk_target or self.f_server
        field.SetValue(field.GetValue()[:-1])
        field.SetInsertionPointEnd()

    def _osk_press(self, row, index):
        if self.osk_target is None:
            self.osk_target = self.f_server
        if row < OSK_ROWS:
            self._osk_type(OSK_LAYERS[self.osk_layer][row][index])
            if self.osk_layer == "upper":             # shift applies to one letter, like a phone
                self.osk_layer = "lower"
                self._osk_relabel()
            return
        name = OSK_SPECIAL[index][0]
        if name == "Shift":
            self.osk_layer = "lower" if self.osk_layer == "upper" else "upper"
            self._osk_relabel()
        elif name == "#+=":
            self.osk_layer = "lower" if self.osk_layer == "symbols" else "symbols"
            self._osk_relabel()
        elif name == "Space":
            self._osk_type(" ")
        elif name == "Delete":
            self._osk_erase()
        elif name == "Done":
            self._osk_close()
        else:
            self._osk_type(name)

    def _osk_move(self, row, index, d_row, d_col):
        if d_col:
            count = len(OSK_SPECIAL) if row == OSK_ROWS else OSK_COLS
            index = max(0, min(count - 1, index + d_col))
        else:
            new_row = row + d_row
            if new_row < 0:
                self._osk_close()
                return
            if new_row > OSK_ROWS:
                return
            if new_row == OSK_ROWS:                   # character row -> special row
                index = next(i for i, (_l, first, width) in enumerate(OSK_SPECIAL)
                             if first <= index < first + width)
            elif row == OSK_ROWS:                     # special row -> character row
                index = OSK_SPECIAL[index][1]
            row = new_row
        (self.special_buttons[index] if row == OSK_ROWS else self.key_buttons[row][index]).SetFocus()

    # ---- home screen ----------------------------------------------------
    def _set_home(self, views):
        api = self.api
        self.entries = [{"kind": "section", "title": view.get("Name", "Library"),
                         "fn": (lambda v=view: api.children(v))} for view in views]
        self.entries += [{"kind": "action", "name": "settings", "title": "Settings"},
                         {"kind": "action", "name": "exit", "title": "Exit"}]
        self.p_home.set_titles([entry["title"] for entry in self.entries])

    def _home_open(self):
        index = self.p_home.sel
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        if entry["kind"] == "section":
            self.stack = [{"title": entry["title"], "fn": entry["fn"], "row": 0}]
            self._show_page(self.p_browse)
            self.view.SetFocus()
            self._load()
        elif entry["name"] == "settings":
            self._open_settings()
        elif entry["name"] == "exit":
            self.Close()

    def _go_home(self):
        self.generation += 1             # drop any list still loading
        self.stack = []
        self._show_page(self.p_home)
        self.p_home.SetFocus()

    def _clock_tick(self):
        minute = time.strftime("%H:%M")
        if minute != self.clock_minute:
            self.clock_minute = minute
            if self.page is self.p_home:
                self.p_home.Refresh()

    # ---- settings -------------------------------------------------------
    def _open_settings(self, row=0):
        size = self.config["text_size"] if self.config["text_size"] in TEXT_SIZES else "large"

        def answer(index):
            if index == 0:
                self.config["text_size"] = TEXT_SIZE_ORDER[(TEXT_SIZE_ORDER.index(size) + 1) % len(TEXT_SIZE_ORDER)]
                self.config.save()
                self._apply_look()
                self._open_settings(0)
            elif index == 1:
                self.config["artwork"] = not self.config["artwork"]
                self.config.save()
                self._open_settings(1)
            elif index == 2:
                self._open_folder(str(mpv_dir()))
                self._open_settings(2)
            elif index == 3:
                self._open_about()
            elif index == 4:
                self.config["token"] = ""
                self.config.save()
                self.api = None
                self._show_login()
            else:
                self._go_home()

        self._ask("Settings", [f"Text size: {size.capitalize()}",
                               f"Artwork: {'Posters' if self.config['artwork'] else 'Text only'}",
                               "mpv folder", "About", "Sign out"], answer, row=row)

    def _open_about(self):
        detail = (f"Version: {VERSION}\n\n"
                  "A calm Jellyfin client that plays through mpv.\n"
                  "The name blends migraine and Jellyfin.\n\n"
                  "Free software under the GNU General Public License, version 3 or later.\n"
                  "Built with wxPython and libmpv.\n\n"
                  f"Settings are stored in {app_dir()}")
        self._ask(APP_NAME, ["Back"], lambda _index: self._open_settings(3), detail=detail)

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening the user's own config folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError:
            pass

    # ---- full-page questions (used instead of pop-up dialogs) -----------
    def _ask(self, title, options, callback, detail="", row=0):
        self.choice_callback = callback
        self.choice_title.SetLabel(title)
        self.choice_detail.SetLabel(detail)
        self.choice_detail.Show(bool(detail))
        if detail:
            self.choice_detail.Wrap(self.choice_col.GetMinSize().width)
        self.options.set_rows(options)
        self._show_page(self.p_choice)
        self.options.select(min(row, len(options) - 1), notify=False)
        self.options.SetFocus()

    def _choice_finish(self, index):
        callback, self.choice_callback = self.choice_callback, None
        if callback:
            callback(index)

    # ---- library screen -------------------------------------------------
    def _set_status(self, text):
        self.status.SetLabel(text)
        self.p_browse.Layout()

    def _set_items(self, items):
        self.items = list(items)
        self.art_generation += 1
        self.art_requested = set()
        if self.config["artwork"]:
            self.wide = bool(self.items) and all(item.get("Type") == "Episode" for item in self.items)
            size = self.FromDIP(wx.Size(*(WIDE_SIZE if self.wide else POSTER_SIZE)))
            self.thumb = (size.width, size.height)
            self.view.set_rows([row_label(item) for item in self.items], grid=True, thumb=self.thumb)
        else:
            self.view.set_rows([row_label(item) for item in self.items])
        self.info.SetValue("")

    def _load(self, row=0):
        if not self.stack or self.api is None:
            return
        self.generation += 1
        generation = self.generation
        self.restore_row = row
        self.crumb.SetLabel("  /  ".join(entry["title"] for entry in self.stack))
        self._set_items([])
        self._set_status("Loading")
        run_async(self.stack[-1]["fn"], lambda items, error: self._loaded(generation, items, error))

    def _loaded(self, generation, items, error):
        if generation != self.generation:
            return
        if error:
            self._set_status(str(error))
            return
        self._set_items(items or [])
        self._set_status("" if self.items else "Nothing here yet")
        if self.items:
            self.view.select(min(self.restore_row, len(self.items) - 1))

    def _current_item(self):
        index = self.view.sel
        return self.items[index] if 0 <= index < len(self.items) else None

    def _show_info(self):
        data = self._current_item()
        if data is None:
            self.info.SetValue("")
            return
        self._set_status(f"{self.view.sel + 1} of {len(self.items)}")
        parts = []
        if data.get("SeriesName") and data.get("Type") == "Episode":
            parts.append(data["SeriesName"])
        if data.get("ProductionYear") and data.get("Type") != "Movie":
            parts.append(str(data["ProductionYear"]))
        if data.get("RunTimeTicks"):
            parts.append(f"{max(int(data['RunTimeTicks'] / 600_000_000), 1)} min")
        if data.get("OfficialRating"):
            parts.append(data["OfficialRating"])
        head = "   |   ".join(parts)
        overview = data.get("Overview") or ""
        self.info.SetValue(f"{head}\n{overview}" if head else overview)

    def _request_art(self):
        """Every third of a second: fetch posters for the rows currently on screen."""
        if (self.page is not self.p_browse or not self.config["artwork"] or not self.items
                or self.api is None):
            return
        first, last = self.view.visible_range()
        api, generation = self.api, self.art_generation
        width, height = self.thumb
        for row in range(first, min(len(self.items), last)):
            if row in self.art_requested:
                continue
            self.art_requested.add(row)
            url = api.image_url(self.items[row], self.wide, width, height)
            if not url:
                continue

            def done(data, error, row=row):
                if error or not data or generation != self.art_generation:
                    return
                bitmap = self._poster(data)
                if bitmap is not None:
                    self.view.set_image(row, bitmap)

            run_async(lambda url=url: api.fetch(url), done)

    def _poster(self, data):
        """Decode a downloaded picture and scale it to fit the poster box, never stretched."""
        image = wx.Image(io.BytesIO(data))
        if not image.IsOk():
            return None
        width, height = self.thumb
        scale = min(width / image.GetWidth(), height / image.GetHeight())
        image = image.Scale(max(1, int(image.GetWidth() * scale)), max(1, int(image.GetHeight() * scale)),
                            wx.IMAGE_QUALITY_HIGH)
        return wx.Bitmap(image)

    def _back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self._load(row=self.stack[-1].get("row", 0))
        else:
            self._go_home()

    def _activate(self):
        data = self._current_item()
        if data is None:
            return
        self.stack[-1]["row"] = self.view.sel
        if data.get("IsFolder"):
            api = self.api
            self.stack.append({"title": data.get("Name", ""), "fn": (lambda: api.children(data)), "row": 0})
            self._load()
            return
        if data.get("MediaType") != "Video":
            self._set_status("This kind of item is not supported yet")
            return
        position = ((data.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / 10_000_000
        if position < 30:
            self._play(data, 0)
            return

        def answer(index):
            self._show_page(self.p_browse)
            self.view.SetFocus()
            if index == 0:
                self._play(data, position)
            elif index == 1:
                self._play(data, 0)

        self._ask(item_label(data), [f"Resume from {clock(position)}", "Play from the beginning", "Cancel"], answer)

    # ---- playback -------------------------------------------------------
    def _play(self, data, start):
        api = self.api
        title = item_label(data)
        if data.get("SeriesName") and data.get("Type") == "Episode":
            title = f"{data['SeriesName']} - {title}"
        try:
            self.player.play(api.stream_url(data), title, start)
        except Exception as exc:
            self._set_status(f"mpv could not start: {exc}")
            return
        self.now_playing = data
        self.ticks = 0
        self.playing_text.SetLabel(f"Playing in mpv\n\n{title}\n\n"
                                   "All mpv keys work as usual.  q returns to the library.")
        self._show_page(self.p_playing)
        self.p_playing.SetFocus()
        self.play_timer.Start(2000)
        run_async(lambda: api.report("start", data["Id"], start), lambda _r, _e: None)

    def _playback_tick(self):
        if self.now_playing is None:
            self.play_timer.Stop()
            return
        if not self.player.alive() or self.player.stalled():
            position = self.player.pos
            self.player.abandon()
            self._playback_finished(position)
            return
        self.ticks += 1
        if self.ticks % 5 == 0 and self.player.pos is not None:
            api, item_id = self.api, self.now_playing["Id"]
            position, paused = self.player.pos, self.player.paused
            run_async(lambda: api.report("progress", item_id, position, paused), lambda _r, _e: None)

    def _playback_finished(self, position):
        data, self.now_playing = self.now_playing, None
        self.play_timer.Stop()
        if data is None:
            return
        self._show_page(self.p_browse)
        self.Raise()
        self.view.SetFocus()
        if position is None:
            self._set_status("Playback did not start. Check the file and your mpv.conf.")
            return
        api = self.api
        row = self.stack[-1].get("row", 0) if self.stack else 0

        def done(_result, _error):
            # Reload so resume points and watched marks are current.
            if self.stack and self.now_playing is None and self.page is self.p_browse:
                self._load(row=row)

        run_async(lambda: api.report("stop", data["Id"], position), done)

    # ---- input ----------------------------------------------------------
    def _on_char_hook(self, event):
        if not self._handle_key(event.GetKeyCode(), event):
            event.Skip()

    def _handle_key(self, code, event=None):
        """Remote-style navigation. Returns True when the key was used; otherwise Windows handles it."""
        focus = wx.Window.FindFocus()
        focus_id = focus.GetId() if focus is not None else None
        if self.page is self.p_playing:
            # Safety net: if mpv's window did not take keyboard focus, pass keys along.
            name = self._mpv_key(code, event)
            if name:
                self.player.command("keypress", name)
            return True
        if self.page is self.p_login:
            position = self.key_pos.get(focus_id)
            if position is not None:
                moves = {wx.WXK_LEFT: (0, -1), wx.WXK_RIGHT: (0, 1), wx.WXK_UP: (-1, 0), wx.WXK_DOWN: (1, 0)}
                if code in moves:
                    self._osk_move(position[0], position[1], *moves[code])
                elif code in ENTER_KEYS:
                    self._osk_press(*position)
                elif code == wx.WXK_BACK:
                    self._osk_erase()
                elif code == wx.WXK_ESCAPE:
                    self._osk_close()
                else:
                    return False
                return True
            order = self.fields + [self.b_connect]
            order_ids = [w.GetId() for w in order]
            if focus_id in order_ids:
                index = order_ids.index(focus_id)
                if code == wx.WXK_DOWN and index + 1 < len(order):
                    order[index + 1].SetFocus()
                elif code == wx.WXK_UP and index > 0:
                    order[index - 1].SetFocus()
                elif code in ENTER_KEYS:
                    if index == len(order) - 1:
                        self._submit_login()
                    else:
                        self._osk_open(order[index])
                else:
                    return False
                return True
            return False
        if self.page is self.p_home:
            if code == wx.WXK_LEFT:
                self.p_home.move(-1)
            elif code == wx.WXK_RIGHT:
                self.p_home.move(1)
            elif code in ENTER_KEYS or code == wx.WXK_DOWN:
                if code in ENTER_KEYS or self.entries[self.p_home.sel]["kind"] == "section":
                    self._home_open()            # Down opens a library but never Settings or Exit
            return True
        if self.page is self.p_choice:
            if code == wx.WXK_UP:
                self.options.move(0, -1)
            elif code == wx.WXK_DOWN:
                self.options.move(0, 1)
            elif code in ENTER_KEYS:
                self._choice_finish(self.options.sel)
            elif code in BACK_KEYS:
                self._choice_finish(-1)
            return True
        if self.page is self.p_browse:
            view = self.view
            if code in ENTER_KEYS:
                self._activate()
            elif code in BACK_KEYS:
                self._back()
            elif code == wx.WXK_LEFT:
                if not view.move(-1, 0) and not view.grid:
                    self._back()                 # in the text list, Left goes back a level
            elif code == wx.WXK_RIGHT:
                view.move(1, 0)
            elif code == wx.WXK_UP:
                view.move(0, -1)
            elif code == wx.WXK_DOWN:
                view.move(0, 1)
            elif code == wx.WXK_PAGEUP:
                view.page(-1)
            elif code == wx.WXK_PAGEDOWN:
                view.page(1)
            elif code == wx.WXK_HOME:
                view.select(0)
            elif code == wx.WXK_END:
                view.select(len(self.items) - 1)
            elif 47 < code < 91 and not (event and (event.ControlDown() or event.AltDown())):
                view.jump(chr(code))             # a letter or digit jumps to the next matching title
            return True
        return False

    @staticmethod
    def _mpv_key(code, event):
        name = MPV_KEYS.get(code)
        shift = bool(event and event.ShiftDown())
        if name is None:
            if not 32 < code < 127:
                return None
            name = chr(code)
            if name.isalpha():
                name = name.upper() if shift else name.lower()
        elif shift:
            name = "Shift+" + name
        if event and event.AltDown():
            name = "Alt+" + name
        if event and event.ControlDown():
            name = "Ctrl+" + name
        return name

    def _poll_gamepad(self):
        for button in self.gamepad.poll():
            if self.now_playing is not None:
                command = {"A": ("cycle", "pause"), "START": ("cycle", "pause"), "B": ("stop",),
                           "BACK": ("stop",), "LEFT": ("seek", "-10"), "RIGHT": ("seek", "10"),
                           "UP": ("seek", "60"), "DOWN": ("seek", "-60"),
                           "X": ("cycle", "sub"), "Y": ("show-progress",)}.get(button)
                if command:
                    self.player.command(*command)
                continue
            code = {"UP": wx.WXK_UP, "DOWN": wx.WXK_DOWN, "LEFT": wx.WXK_LEFT, "RIGHT": wx.WXK_RIGHT,
                    "A": wx.WXK_RETURN, "START": wx.WXK_RETURN, "B": wx.WXK_ESCAPE,
                    "BACK": wx.WXK_ESCAPE}.get(button)
            if code is not None:
                self._handle_key(code)

    def _on_close(self, _event):
        for timer in (self.play_timer, self.pad_timer, self.art_timer, self.clock_timer):
            timer.Stop()
        data, self.now_playing = self.now_playing, None
        if data is not None and self.api is not None and self.player.pos is not None:
            try:
                self.api.report("stop", data["Id"], self.player.pos, timeout=3)
            except Exception:
                pass
        self.player.shutdown()
        shutdown_pool()
        self.Destroy()


def main():
    config = Config()
    if os.name == "nt":
        # Tell Windows this app scales itself, so Windows never stretches (blurs) the window.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = wx.App(False)
    wx.Log.EnableLogging(False)     # no pop-up boxes for things like an unreadable poster
    frame = MainFrame(config)
    if "--windowed" in sys.argv:
        frame.Show()
    elif config["fullscreen"]:
        frame.Show()
        frame.ShowFullScreen(True)
    else:
        frame.Maximize()
        frame.Show()
    frame.start()
    app.MainLoop()


if __name__ == "__main__":
    main()
