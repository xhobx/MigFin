# MigFin

A calm, 10-foot (TV-style) client for [Jellyfin](https://github.com/jellyfin/jellyfin) that plays
everything through [mpv](https://github.com/mpv-player/mpv).

It is written for people whose migraines or motion sickness are triggered by modern rendering:
temporal dithering, FRC, frame interpolation, HDR switching, and animated interfaces. MigFin
avoids all of those in the parts it controls.

The name is a blend of **mig**raine and Jelly**fin**.

**Status: alpha.** Early build. The build number is shown in the bottom corner of the home screen and in
Settings > About, and matches the release it came from. The author cannot run it on Windows; expect bugs and please
report them.

## What it does to stay still

Interface

- A home screen in the spirit of Kodi's classic layout: a dark band low on the screen carries the
  menu words (your libraries, Settings, Exit). Left and Right move the highlight; the words
  themselves stay where they are.
- A library screen of posters, or plain text rows (Settings > Artwork).
- Navy blue throughout, with dark red for the highlight.
- Drawn with ordinary Windows GDI calls on the CPU. Flat, opaque fills only: no gradients,
  transparency, blur or shadows.
- Nothing animates. Scrolling happens a whole row at a time, in one step. Questions appear as
  full pages, not pop-up windows.

Video (re-applied before every playback while `enforce_safe` is on)

| mpv option | value | why |
| --- | --- | --- |
| `dither-depth`, `temporal-dither` | `no` | no dithering of any kind |
| `interpolation` | `no` | no frame blending or motion smoothing |
| `video-sync` | `audio` | video is never retimed to the display |
| `deband` | `no` | debanding adds noise that changes every frame |
| `target-colorspace-hint` | `no` | the display is never switched into HDR |
| `hdr-compute-peak` | `no` | HDR video is tone-mapped to SDR with fixed brightness |
| `d3d11-output-format` | `rgba8` | 8-bit output on Windows |

With dithering off, 10-bit video shown at 8 bits can show visible banding in smooth areas such as
skies. That is the expected trade.

### What it cannot control

Temporal dithering or FRC applied by the **graphics driver** or the **monitor panel** happens
after any application has finished drawing, so no app can switch it off. Use a display and
driver setup you already know is comfortable (8-bit output, HDR and VRR off). Windows also
animates windows opening and closing; turn that off under
Settings > Accessibility > Visual effects > Animation effects.

## Install (Windows)

1. Download `MigFin-windows-x64.zip` from the Releases page.
2. Unzip anywhere and run `MigFin\MigFin.exe`.
3. Enter your server address, username and password. Press Enter (A on a controller) on a box
   to use the on-screen keyboard, or type on a real keyboard.

If it reports that libmpv cannot be loaded, update your graphics driver: recent libmpv builds
need `vulkan-1.dll`, which GPU drivers install.

## Controls

| | Keyboard / remote | Controller |
| --- | --- | --- |
| Home screen | Left / Right, then Enter (or Down) | d-pad, then A |
| Library | arrow keys, Page Up / Down, Home / End | d-pad or left stick |
| Jump to a title | type its first letter | |
| Open or play | Enter | A |
| Back | Esc or Backspace | B |
| During playback | every mpv key | A pause, B stop, d-pad seek, X subtitles |

During playback mpv runs in its own fullscreen window with its default key bindings, on-screen
controller and scripts, exactly like stock mpv. One difference: `q`, `Q` and closing the window
stop the video and return to the library.

## mpv settings, shortcuts and scripts

MigFin keeps its own mpv folder, opened from **Settings > mpv folder**
(`%APPDATA%\MigFin\mpv`). It is a normal mpv config folder:

- `mpv.conf` - created on first run with the comfort profile
- `input.conf` - add your own key bindings
- `scripts\` and `script-opts\` - drop in any mpv script

To use your existing mpv setup, copy your files into that folder.

To let your own `mpv.conf` win over the comfort profile, set `"enforce_safe": false` in
`%APPDATA%\MigFin\config.json`.

### Black bars on 21:9 screens

[dynamic-crop.lua](https://github.com/Ashyni/mpv-scripts/blob/master/dynamic-crop.lua) by Ashyni
crops black bars automatically. It is **not included** because the script carries no licence
notice. Download it from the author's GitHub page and copy it into the `scripts` folder
(**Settings > mpv folder** opens the right place). `Shift+C` toggles
it. It needs `hwdec=no` or a `-copy` mode; the default `mpv.conf` sets `hwdec=auto-copy`.

## About the interface

The interface is a small amount of custom drawing on top of wxPython, which hands the drawing to
Windows GDI. Text size (large, medium, small) and Artwork (posters or text only) are under
**Settings** on the home screen.

## Run from source

```
pip install -r requirements.txt
python main.py            # add --windowed for a normal window
```

You need libmpv: on Windows put `libmpv-2.dll` next to `main.py`
(from [shinchiro's builds](https://github.com/shinchiro/mpv-winbuild-cmake/releases), the
`mpv-dev-x86_64-<date>` archive); on Linux install your distribution's libmpv package.

## Building the exe

The whole app is one file, `main.py`. Every push to `main` runs `.github/workflows/build.yml`, which builds the exe with PyInstaller,
bundles libmpv and publishes it as a pre-release. No local build tools are needed.

## Known limits in this alpha

- Direct play only; the server never transcodes. mpv plays almost anything, but there is no
  fallback for very slow connections.
- External subtitle files stored beside the video are not loaded yet. Embedded tracks work.
- No search, no music or photo libraries.
- No automatic next episode.
- The sign-in token is stored unencrypted in `config.json`.

## Roadmap

1. Fixes from first real-world testing
2. External subtitles and remembered audio/subtitle language
3. Search, using the on-screen keyboard
4. Optional "play next episode" prompt (static, never a countdown animation)

## Licence and credits

MigFin is free software under the GNU General Public License, version 3 or later. See `LICENSE`.

It builds on [mpv](https://mpv.io) (GPL/LGPL), [python-mpv](https://github.com/jaseg/python-mpv)
and [wxPython](https://wxpython.org) (wxWindows licence). It talks to Jellyfin over the public
API and shares no code or artwork with Jellyfin, jellyfin-mpv-shim or Kodi. MigFin is not
affiliated with any of those projects.

Background on the triggers this project avoids: [LEDStrain](https://ledstrain.org/),
r/ScreenSensitive, r/PWM_Sensitive.
