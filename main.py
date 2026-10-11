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
BUILD = "9"
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


# ==== interface: real Windows controls through wxPython ==================
# Every control below is one of Windows' own (list box, edit box, push button,
# static text), created and painted by Windows itself with the system font
# renderer and system colours. MigFin sets no colours and draws nothing.

TEXT_SIZES = {"large": 20, "medium": 14, "small": 10}
TEXT_SIZE_ORDER = ["large", "medium", "small"]
POSTER_SIZE = (150, 225)      # at 100% display scaling; Windows scaling is applied on top
WIDE_SIZE = (256, 144)        # episode stills
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
    """One line of the library list: title plus watched / resume state."""
    label = item_label(item)
    user = item.get("UserData") or {}
    if user.get("PlaybackPositionTicks"):
        label += f"     (resume at {clock(user['PlaybackPositionTicks'] / 10_000_000)})"
    elif user.get("Played"):
        label += "     (watched)"
    elif user.get("UnplayedItemCount"):
        label += f"     ({user['UnplayedItemCount']} unwatched)"
    return label


class QuietText(wx.TextCtrl):
    """Read-only text that wraps by itself and never takes keyboard focus."""

    def AcceptsFocus(self):
        return False

    def AcceptsFocusFromKeyboard(self):
        return False


class MainFrame(wx.Frame):
    def __init__(self, config):
        super().__init__(None, title=APP_NAME, size=(1600, 900))
        self.config = config
        self.api = None
        self.entries = []            # menu bar entries
        self.menu_buttons = []       # one native toggle button per entry
        self.menu_index = -1
        self.image_list = None
        self.thumb = (0, 0)
        self.wide = False
        self.art_generation = 0
        self.art_requested = set()
        self.items = []              # Jellyfin items behind the rows of the library list
        self.stack = []              # navigation trail for the library list
        self.generation = 0          # bumps on every load; stale replies are dropped
        self.focus_after_load = False
        self.restore_row = 0
        self.now_playing = None
        self.ticks = 0
        self.choice_callback = None
        self.osk_target = None
        self.osk_layer = "lower"
        self.page = None
        self.player = Player(enforce_safe=bool(config["enforce_safe"]),
                             on_finished=self._playback_finished)

        self._build_login()
        self._build_browse()
        self._build_choice()
        self._build_playing()
        self.pages = [self.p_login, self.p_browse, self.p_choice, self.p_playing]
        sizer = wx.BoxSizer(wx.VERTICAL)
        for page in self.pages:
            sizer.Add(page, 1, wx.EXPAND)
            page.Hide()
        self.SetSizer(sizer)

        self.side_timer = wx.Timer(self)
        self.play_timer = wx.Timer(self)
        self.pad_timer = wx.Timer(self)
        self.art_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._side_settled(), self.side_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._playback_tick(), self.play_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._poll_gamepad(), self.pad_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._request_art(), self.art_timer)
        self.art_timer.Start(350)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.gamepad = Gamepad()
        if self.gamepad.available:
            self.pad_timer.Start(40)
        self._apply_fonts()

    def start(self):
        self._show_focus_cues()
        if self.config["token"] and self.config["server"]:
            self.api = Jellyfin(self.config["server"], self.config["device_id"],
                                self.config["token"], self.config["user_id"])
            self._start_session()
        else:
            self._show_login()

    def _show_focus_cues(self):
        """Ask Windows to draw focus rectangles from the start (it hides them until Tab is used)."""
        if os.name == "nt":
            try:
                ctypes.windll.user32.SendMessageW(self.GetHandle(), 0x0127, (1 << 16) | 2, 0)
            except Exception:
                pass

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

    def _build_login(self):
        p = self.p_login = wx.Panel(self)
        self.login_title = wx.StaticText(p, label=APP_NAME)
        self.login_hint_text = ("Sign in to your Jellyfin server. Press Enter (A on a controller) on a box "
                                "to type with the on-screen keyboard, or just type on a real keyboard.")
        self.login_hint = wx.StaticText(p, label=self.login_hint_text)
        self.f_server = wx.TextCtrl(p)
        self.f_user = wx.TextCtrl(p)
        self.f_pass = wx.TextCtrl(p, style=wx.TE_PASSWORD)
        self.fields = [self.f_server, self.f_user, self.f_pass]
        self.b_connect = wx.Button(p, label="Connect")
        self.b_connect.Bind(wx.EVT_BUTTON, lambda _e: self._submit_login())
        self.login_error = wx.StaticText(p, label="")

        grid = wx.GridBagSizer(4, 4)
        self.key_buttons = []        # 4 rows of 10 character keys
        self.special_buttons = []
        self.key_pos = {}            # button id -> (row, index)
        for r in range(OSK_ROWS):
            line = []
            for c in range(OSK_COLS):
                button = wx.Button(p, label="")
                button.Bind(wx.EVT_BUTTON, lambda _e, r=r, c=c: self._osk_press(r, c))
                grid.Add(button, pos=(r, c), flag=wx.EXPAND)
                self.key_pos[button.GetId()] = (r, c)
                line.append(button)
            self.key_buttons.append(line)
        for index, (label, first, width) in enumerate(OSK_SPECIAL):
            button = wx.Button(p, label=label)
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
            col.Add(wx.StaticText(p, label=caption), 0, wx.BOTTOM, 2)
            col.Add(field, 0, wx.EXPAND | wx.BOTTOM, 10)
        col.Add(self.b_connect, 0, wx.BOTTOM, 8)
        col.Add(self.login_error, 0, wx.EXPAND | wx.BOTTOM, 14)
        col.Add(grid, 0, wx.EXPAND)
        self._centred(p, col, 1, 1)

    def _build_browse(self):
        p = self.p_browse = wx.Panel(self)
        self.menu_sizer = wx.WrapSizer(wx.HORIZONTAL)   # the horizontal menu bar; wraps if it is long
        self.build_label = wx.StaticText(p, label=VERSION)
        self.crumb = wx.StaticText(p, label="")
        self.status = wx.StaticText(p, label="")
        self.content = wx.ListBox(p, style=wx.LB_SINGLE)
        self.info = QuietText(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        self.content.Bind(wx.EVT_LISTBOX, lambda _e: self._show_info())
        self.content.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._activate())
        # Posters use the Windows list-view control in its icon view, as File Explorer does.
        self.grid = wx.ListCtrl(p, style=wx.LC_ICON | wx.LC_SINGLE_SEL | wx.LC_AUTOARRANGE)
        self.grid.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda _e: self._show_info())
        self.grid.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self._activate())
        self.grid.Hide()

        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(self.crumb, 1, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.status, 0, wx.ALIGN_CENTER_VERTICAL)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        footer.Add(self.info, 1, wx.EXPAND)
        footer.Add(self.build_label, 0, wx.ALIGN_BOTTOM | wx.LEFT, 16)
        page = wx.BoxSizer(wx.VERTICAL)
        page.Add(self.menu_sizer, 0, wx.EXPAND | wx.BOTTOM, 14)
        page.Add(header, 0, wx.EXPAND | wx.BOTTOM, 8)
        page.Add(self.content, 1, wx.EXPAND | wx.BOTTOM, 8)
        page.Add(self.grid, 1, wx.EXPAND | wx.BOTTOM, 8)
        page.Add(footer, 0, wx.EXPAND)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(page, 1, wx.EXPAND | wx.ALL, 16)
        p.SetSizer(outer)

    def _build_choice(self):
        p = self.p_choice = wx.Panel(self)
        self.choice_title = wx.StaticText(p, label="")
        self.choice_detail = wx.StaticText(p, label="")
        self.options = wx.ListBox(p, style=wx.LB_SINGLE)
        self.options.Bind(wx.EVT_LISTBOX_DCLICK, lambda _e: self._choice_finish(self.options.GetSelection()))
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

    def _apply_fonts(self):
        points = TEXT_SIZES.get(self.config["text_size"], TEXT_SIZES["large"])
        base = wx.Font(wx.FontInfo(points).FaceName("Segoe UI"))
        big = wx.Font(wx.FontInfo(round(points * 1.4)).FaceName("Segoe UI").Bold())

        def walk(window):
            window.SetFont(base)
            for child in window.GetChildren():
                walk(child)

        walk(self)
        for title in (self.login_title, self.crumb, self.choice_title):
            title.SetFont(big)
        dc = wx.ClientDC(self)
        dc.SetFont(base)
        char_w, char_h = dc.GetTextExtent("M")
        key = (int(char_h * 2.2), int(char_h * 1.9))
        for button in [b for line in self.key_buttons for b in line] + self.special_buttons:
            button.SetMinSize(key)
        column = key[0] * OSK_COLS + 4 * (OSK_COLS - 1)
        self.login_col.SetMinSize((column, -1))
        self.login_hint.SetLabel(self.login_hint_text)
        self.login_hint.Wrap(column)
        self.base_font = base
        self.info.SetMinSize((-1, int(char_h * 4.4)))
        self.choice_col.SetMinSize((char_w * 34, -1))
        self.options.SetMinSize((-1, int(char_h * 1.5 * 6)))
        self.choice_detail.Wrap(char_w * 34)
        for page in (self.p_login, self.p_browse, self.p_choice, self.p_playing):
            page.Layout()
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
        self._show_page(self.p_browse)
        self._set_menu([])
        self._set_items([])
        self.crumb.SetLabel("")
        self._set_status("Loading libraries")
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
            self._fill_side(views or [])

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

    # ---- menu bar -------------------------------------------------------
    def _set_menu(self, entries):
        """Rebuild the row of native toggle buttons across the top."""
        self.entries = entries
        self.menu_index = -1
        self.menu_sizer.Clear(True)          # destroys the old buttons
        self.menu_buttons = []
        for index, entry in enumerate(entries):
            button = wx.ToggleButton(self.p_browse, label=entry["title"].replace("&", "&&"))
            button.SetFont(self.base_font)
            button.Bind(wx.EVT_TOGGLEBUTTON, lambda _e, i=index: self._menu_clicked(i))
            self.menu_sizer.Add(button, 0, wx.RIGHT | wx.BOTTOM, 8)
            self.menu_buttons.append(button)
        self.p_browse.Layout()

    def _fill_side(self, views):
        api = self.api
        entries = [{"kind": "section", "title": view.get("Name", "Library"),
                    "fn": (lambda v=view: api.children(v))} for view in views]
        entries += [
            {"kind": "action", "name": "settings", "title": "Settings",
             "hint": "Text size, mpv folder, about and sign out. Press Enter to open."},
            {"kind": "action", "name": "exit", "title": "Exit",
             "hint": f"Press Enter to close {APP_NAME}."},
        ]
        self._set_menu(entries)
        self._set_status("")
        self._select_menu(0, settle_now=True)

    def _select_menu(self, index, settle_now=False):
        """Move to a menu entry: it shows as pressed, takes focus, and its list loads."""
        if not self.menu_buttons:
            return
        index = max(0, min(len(self.menu_buttons) - 1, index))
        self.menu_index = index
        for i, button in enumerate(self.menu_buttons):
            button.SetValue(i == index)
        self.menu_buttons[index].SetFocus()
        if settle_now:
            self.side_timer.Stop()
            self._side_settled()
        else:
            self.side_timer.StartOnce(180)

    def _focus_menu(self):
        if self.menu_buttons:
            self.menu_buttons[max(self.menu_index, 0)].SetFocus()

    def _menu_clicked(self, index):
        self._select_menu(index, settle_now=True)
        self._side_chosen(True)

    def _side_entry(self):
        index = self.menu_index
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def _side_settled(self):
        entry = self._side_entry()
        if entry is None or self.page is not self.p_browse:
            return
        self.generation += 1
        if entry["kind"] == "action":
            self.stack = []
            self._set_items([])
            self.crumb.SetLabel(entry["title"])
            self._set_status("")
            self.info.SetValue(entry["hint"])
            return
        self.stack = [{"title": entry["title"], "fn": entry["fn"], "row": 0}]
        self._load(focus=False)

    def _side_chosen(self, allow_actions):
        entry = self._side_entry()
        if entry is None:
            return
        if entry["kind"] == "section":
            if self.side_timer.IsRunning():           # selection moved a moment ago: load first
                self.side_timer.Stop()
                self._side_settled()
                self.focus_after_load = True
            elif self.items:
                if self._selection() < 0:
                    self._select(0)
                self._view().SetFocus()
                self._show_info()
            return
        if not allow_actions:                         # Down never triggers Settings or Exit
            return
        if entry["name"] == "exit":
            self.Close()
        elif entry["name"] == "settings":
            self._open_settings()

    # ---- settings -------------------------------------------------------
    def _open_settings(self, row=0):
        size = self.config["text_size"] if self.config["text_size"] in TEXT_SIZES else "large"

        def answer(index):
            if index == 0:
                self.config["text_size"] = TEXT_SIZE_ORDER[(TEXT_SIZE_ORDER.index(size) + 1) % len(TEXT_SIZE_ORDER)]
                self.config.save()
                self._apply_fonts()
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
                self._show_page(self.p_browse)
                self._focus_menu()
                self._side_settled()

        self._ask("Settings", [f"Text size: {size.capitalize()}",
                               f"Artwork: {'Posters' if self.config['artwork'] else 'Text only'}",
                               "mpv folder", "About", "Sign out"], answer, row=row)

    def _open_about(self):
        detail = (f"Version: {VERSION}\n\n"
                  "A calm Jellyfin client that plays through mpv.\n"
                  "The name blends migraine and Jellyfin.\n\n"
                  "The interface is made only of Windows' own classic controls.\n\n"
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
        self.options.Set(options)
        self.options.SetSelection(min(row, len(options) - 1))
        self._show_page(self.p_choice)
        self.options.SetFocus()

    def _choice_finish(self, index):
        callback, self.choice_callback = self.choice_callback, None
        if callback:
            callback(index)

    # ---- library list ---------------------------------------------------
    def _set_status(self, text):
        self.status.SetLabel(text)
        self.p_browse.Layout()

    def _artwork(self):
        return bool(self.config["artwork"])

    def _view(self):
        """The control showing the library: the icon view with posters, or the text list."""
        return self.grid if self._artwork() else self.content

    def _selection(self):
        return self.grid.GetFirstSelected() if self._artwork() else self.content.GetSelection()

    def _select(self, index):
        if self._artwork():
            self.grid.Select(index)
            self.grid.Focus(index)
            self.grid.EnsureVisible(index)
        else:
            self.content.SetSelection(index)

    def _set_items(self, items):
        self.items = list(items)
        self.art_generation += 1
        self.art_requested = set()
        art = self._artwork()
        self.grid.Freeze()
        self.grid.DeleteAllItems()
        if art:
            self.wide = bool(self.items) and all(item.get("Type") == "Episode" for item in self.items)
            size = self.FromDIP(wx.Size(*(WIDE_SIZE if self.wide else POSTER_SIZE)))
            self.thumb = (size.width, size.height)
            self.image_list = wx.ImageList(size.width, size.height, False)
            self.image_list.Add(self._canvas(None))             # image 0: blank placeholder
            self.grid.AssignImageList(self.image_list, wx.IMAGE_LIST_NORMAL)
            for row, item in enumerate(self.items):
                self.grid.InsertItem(row, " ".join(row_label(item).split()), 0)
            self.content.Clear()
        else:
            self.content.Set([row_label(item) for item in self.items])
        self.grid.Thaw()
        if self.grid.IsShown() != art:
            self.content.Show(not art)
            self.grid.Show(art)
            self.p_browse.Layout()
        self.info.SetValue("")

    def _canvas(self, data):
        """A fixed-size picture for the icon view: the art centred on a plain background."""
        width, height = self.thumb
        image = None
        if data:
            image = wx.Image(io.BytesIO(data))
            if not image.IsOk():
                return None
            scale = min(width / image.GetWidth(), height / image.GetHeight())
            image = image.Scale(max(1, int(image.GetWidth() * scale)), max(1, int(image.GetHeight() * scale)),
                                wx.IMAGE_QUALITY_HIGH)
        bitmap = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bitmap)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(
            wx.SYS_COLOUR_WINDOW if image is not None else wx.SYS_COLOUR_BTNFACE)))
        dc.Clear()
        if image is not None:
            dc.DrawBitmap(wx.Bitmap(image), (width - image.GetWidth()) // 2, (height - image.GetHeight()) // 2)
        dc.SelectObject(wx.NullBitmap)
        return bitmap

    def _request_art(self):
        """Every third of a second: fetch posters for the rows currently on screen."""
        if (self.page is not self.p_browse or not self._artwork() or not self.items
                or self.api is None or not self.grid.IsShown()):
            return
        size = self.grid.GetClientSize()
        seen = []
        for fx in (0.04, 0.27, 0.5, 0.73, 0.96):
            for fy in (0.05, 0.35, 0.65, 0.95):
                hit = self.grid.HitTest(wx.Point(int(size.width * fx), int(size.height * fy)))
                index = hit[0] if isinstance(hit, (tuple, list)) else hit
                if isinstance(index, int) and index >= 0:
                    seen.append(index)
        if not seen:
            seen = [0]
        first, last = max(0, min(seen) - 12), min(len(self.items) - 1, max(seen) + 12)
        api, generation = self.api, self.art_generation
        width, height = self.thumb
        for row in range(first, last + 1):
            if row in self.art_requested:
                continue
            self.art_requested.add(row)
            url = api.image_url(self.items[row], self.wide, width, height)
            if not url:
                continue

            def done(data, error, row=row):
                if error or not data or generation != self.art_generation:
                    return
                bitmap = self._canvas(data)
                if bitmap is None or row >= self.grid.GetItemCount():
                    return
                index = self.image_list.Add(bitmap)
                if index >= 0:
                    self.grid.SetItemImage(row, index)

            run_async(lambda url=url: api.fetch(url), done)

    def _at_top_row(self):
        index = self._selection()
        if index <= 0:
            return True
        if not self._artwork():
            return False
        try:
            return self.grid.GetItemRect(index).y <= self.grid.GetItemRect(0).y
        except Exception:
            return False

    def _load(self, focus, row=0):
        if not self.stack or self.api is None:
            return
        self.generation += 1
        generation = self.generation
        self.focus_after_load = focus
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
        self._set_status(f"{len(self.items)} items" if self.items else "Nothing here yet")
        if self.focus_after_load:
            self.focus_after_load = False
            if self.items:
                self._select(min(self.restore_row, len(self.items) - 1))
            self._view().SetFocus()
            self._show_info()

    def _current_item(self):
        index = self._selection()
        return self.items[index] if 0 <= index < len(self.items) else None

    def _show_info(self):
        data = self._current_item()
        if data is None:
            self.info.SetValue("")
            return
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

    def _back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self._load(focus=True, row=self.stack[-1].get("row", 0))
        else:
            self._focus_menu()

    def _activate(self):
        data = self._current_item()
        if data is None:
            return
        self.stack[-1]["row"] = self._selection()
        if data.get("IsFolder"):
            api = self.api
            self.stack.append({"title": data.get("Name", ""), "fn": (lambda: api.children(data)), "row": 0})
            self._load(focus=True)
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
            self._view().SetFocus()
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
        self._view().SetFocus()
        if position is None:
            self._set_status("Playback did not start. Check the file and your mpv.conf.")
            return
        api = self.api
        row = self.stack[-1].get("row", 0) if self.stack else 0

        def done(_result, _error):
            # Reload so resume points and watched marks are current.
            if self.stack and self.now_playing is None and self.page is self.p_browse:
                self._load(focus=True, row=row)

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
        if self.page is self.p_choice:
            if code in ENTER_KEYS:
                self._choice_finish(self.options.GetSelection())
            elif code in BACK_KEYS:
                self._choice_finish(-1)
            else:
                return False
            return True
        if self.page is self.p_browse:
            if focus_id == self._view().GetId():
                if code in ENTER_KEYS:
                    self._activate()
                elif code in BACK_KEYS or (code == wx.WXK_LEFT and not self._artwork()):
                    self._back()                      # with posters, Left moves between posters
                elif code == wx.WXK_UP and self._at_top_row():
                    self._focus_menu()                # Up from the top row returns to the menu bar
                else:
                    return False
                return True
            # Otherwise the menu bar has (or is given) the focus.
            ids = [button.GetId() for button in self.menu_buttons]
            if focus_id not in ids:
                self._focus_menu()
            if code == wx.WXK_LEFT:
                self._select_menu(self.menu_index - 1)
            elif code == wx.WXK_RIGHT:
                self._select_menu(self.menu_index + 1)
            elif code in ENTER_KEYS:
                self._side_chosen(True)
            elif code == wx.WXK_DOWN:
                self._side_chosen(False)
            elif code in (wx.WXK_UP, wx.WXK_SPACE):
                pass                                  # nothing above the bar; Space must not un-press a button
            else:
                return False
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

    def _move_selection(self, listbox, delta):
        count = listbox.GetCount()
        if not count:
            return
        index = max(0, min(count - 1, listbox.GetSelection() + delta))
        listbox.SetSelection(index)
        if listbox is self.content:
            self._show_info()

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
            if code is None or self._handle_key(code):
                continue
            focus = wx.Window.FindFocus()
            if focus is not None and focus.GetId() == self.grid.GetId():
                geometry = {wx.WXK_UP: wx.LIST_NEXT_ABOVE, wx.WXK_DOWN: wx.LIST_NEXT_BELOW,
                            wx.WXK_LEFT: wx.LIST_NEXT_LEFT, wx.WXK_RIGHT: wx.LIST_NEXT_RIGHT}.get(code)
                if geometry is not None:
                    target = self.grid.GetNextItem(self._selection(), geometry)
                    if target >= 0:
                        self._select(target)
                        self._show_info()
                continue
            if code not in (wx.WXK_UP, wx.WXK_DOWN):
                continue       # Up / Down in a list: move the selection ourselves
            focus_id = focus.GetId() if focus is not None else None
            for listbox in (self.content, self.options):
                if listbox.GetId() == focus_id:
                    self._move_selection(listbox, -1 if code == wx.WXK_UP else 1)

    def _on_close(self, _event):
        for timer in (self.side_timer, self.play_timer, self.pad_timer, self.art_timer):
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
