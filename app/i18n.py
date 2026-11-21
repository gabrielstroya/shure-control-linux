# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
import gettext
from pathlib import Path
_ = gettext.translation('shure-control-linux', Path(__file__).resolve().parents[1] / 'locale', fallback=True).gettext
