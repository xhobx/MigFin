# MigFin

A calm, 10-foot (TV-style) client for [Jellyfin](https://github.com/jellyfin/jellyfin) that plays
everything through [mpv](https://github.com/mpv-player/mpv).

It is written for people whose migraines or motion sickness are triggered by modern rendering:
temporal dithering, FRC, frame interpolation, HDR switching, and animated interfaces. MigFin
avoids all of those in the parts it controls.

The name is a blend of **mig**raine and Jelly**fin**.

**Status: alpha 0.4.** Early build. The author cannot run it on Windows; expect bugs and please
report them.

## What it does to stay still

Interface
- Drawn on the CPU with flat, opaque colours. No gradients, transparency, blur or shadows.
- Modelled on classic Windows 10 desktop apps: File Explorer's light and dark colours, following
  the Windows app-mode setting (change it in Settings > Comfort lab).
- Text is drawn by GDI with full hinting, the way classic Windows 10 apps draw it, instead of
  Qt's default DirectWrite path. Set `"native_text": false` in `config.json` to switch back.
- Display scaling is rounded to whole numbers so nothing is resampled at fractional sizes.
- Nothing animates. No fades, slides, smooth scrolling, spinners or blinking text cursor.
  Focus moves in a single frame, shown by a solid outline.
- Questions appear as full pages, not pop-up windows.

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
| Move | arrow keys | d-pad or left stick |
| Select | Enter | A |
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

## Comfort lab

**Settings > Comfort lab** holds switches for A/B testing the interface. Each applies
straight away and is remembered. Change one at a time.

| Switch | Options |
| --- | --- |
| Text smoothing | ClearType (Windows setting), greyscale, none |
| Colours | follow Windows, light, dark |
| Artwork | posters, text only |
| Scrolling | row by row, whole pages |
| Size | large, compact |
| Width | full screen width, centre of the screen only |
| Window | fullscreen, ordinary maximised window |

## How this differs from a native Windows 10 app

MigFin's interface is built with Qt 6 Widgets, not a Microsoft UI toolkit. Qt paints every
control itself in software and hands Windows a finished bitmap, so no GPU effects are involved,
but its defaults differ from classic Windows 10 apps (File Explorer, Notepad, Control Panel),
which draw with GDI and the Windows theme engine. From 0.2 MigFin closes the gaps it can: GDI
text, Windows 10 colours, whole-number scaling. Video is a separate path: mpv renders with
Direct3D 11 on the GPU, as nearly every modern video player does.

## Run from source

```
pip install -r requirements.txt
python main.py            # add --windowed for a normal window
```

You need libmpv: on Windows put `libmpv-2.dll` next to `main.py`
(from [shinchiro's builds](https://github.com/shinchiro/mpv-winbuild-cmake/releases), the
`mpv-dev-x86_64-<date>` archive); on Linux install your distribution's libmpv package.

## Building the exe

Every push to `main` runs `.github/workflows/build.yml`, which builds the exe with PyInstaller,
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
5. Poster cache on disk
6. In-app settings page

## Licence and credits

MigFin is free software under the GNU General Public License, version 3 or later. See `LICENSE`.

It builds on [mpv](https://mpv.io) (GPL/LGPL), [python-mpv](https://github.com/jaseg/python-mpv)
and [Qt for Python](https://doc.qt.io/qtforpython/) (LGPL). It talks to Jellyfin over the public
API and shares no code or artwork with Jellyfin, jellyfin-mpv-shim or Kodi. MigFin is not
affiliated with any of those projects.

Background on the triggers this project avoids: [LEDStrain](https://ledstrain.org/),
r/ScreenSensitive, r/PWM_Sensitive.
