# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Jellyfin REST client (standard library only)."""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import APP_NAME, VERSION

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
