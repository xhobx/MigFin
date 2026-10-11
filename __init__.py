# MigFin - a calm 10-foot Jellyfin client built on mpv.
# Copyright (C) 2026 MigFin contributors
# SPDX-License-Identifier: GPL-3.0-or-later
APP_NAME = "MigFin"

# The GitHub build stamps its run number into _build.py, so the number shown in
# the app always matches the "alpha N" release it was downloaded from.
try:
    from ._build import BUILD
except ImportError:
    BUILD = "7"
VERSION = f"alpha {BUILD}"
