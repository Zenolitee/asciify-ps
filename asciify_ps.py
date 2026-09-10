#!/usr/bin/env python3
"""
asciify-ps — turn images into colored ASCII art, with a live preview.

Run it from a source checkout::

    python asciify_ps.py [image]

or use the Windows launcher ``asciify-ps.bat`` / the frozen ``asciify-ps.exe``.
Passing an image path (or dragging a file onto the executable) loads it on start.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, font as tkfont, messagebox, ttk

APP_NAME = "asciify-ps"
APP_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Vendored rendering library.
# In a source checkout it sits next to this file; in a PyInstaller build it is
# frozen as a normal package, so sys.path needs no help there.
# --------------------------------------------------------------------------
if not getattr(sys, "frozen", False):
    _VENDOR_SRC = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "asciify-them", "src"
    )
    if os.path.isdir(_VENDOR_SRC) and _VENDOR_SRC not in sys.path:
        sys.path.insert(0, _VENDOR_SRC)

try:
    from asciify.process import ImgProcessor
    from asciify.renderer import Renderer
    from asciify.utils import CHARSET_PRESETS, DEFAULT_CHARSET

    LIB_ERROR: Exception | None = None
except Exception as _exc:  # pragma: no cover - depends on the environment
    LIB_ERROR = _exc

# Drag & drop is a progressive enhancement: without tkinterdnd2 the app still
# works, the window simply is not a drop target.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False


# ── palette (Catppuccin Mocha) ────────────────────────────────────────────
BG = "#1e1e2e"
BG_ALT = "#181825"
WIDGET = "#313244"
BORDER = "#45475a"
OVERLAY = "#585b70"
TEXT = "#cdd6f4"
SUBTEXT = "#bac2de"
MUTED = "#a6adc8"
ACCENT = "#89b4fa"
ACCENT_HOVER = "#9dc1fb"
ACCENT_PRESS = "#74a0e0"
PINK = "#f5c2e7"
PREVIEW_BG = "#11111b"

WIDTH_MIN, WIDTH_MAX, WIDTH_DEFAULT = 20, 300, 100
COLORS_MIN, COLORS_MAX, COLORS_DEFAULT = 2, 24, 8
DEBOUNCE_MS = 300
FONT_MIN, FONT_MAX = 6, 22
THUMB_MAX = (240, 170)

PREVIEW_FONT_CANDIDATES = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New")

IMAGE_FILETYPES = [
    ("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
    ("All files", "*.*"),
]

# Format label shown in the combobox  ->  internal key
OUTPUT_FORMATS = (
    ("Rice palette  (${cN})", "rice"),
    ("True color  ·  24-bit ANSI", "ansi_truecolor"),
    ("16 colors  ·  classic ANSI", "ansi_16"),
    ("Plain text", "plain"),
)
FORMAT_BY_LABEL = {label: key for label, key in OUTPUT_FORMATS}
FORMAT_LABELS = [label for label, _ in OUTPUT_FORMATS]

# Classic 16-color ANSI foreground codes, rendered with theme colours.
ANSI_16 = {
    30: "#45475a", 31: "#f38ba8", 32: "#a6e3a1", 33: "#f9e2af",
    34: "#89b4fa", 35: "#f5c2e7", 36: "#94e2d5", 37: "#bac2de",
    90: "#585b70", 91: "#f38ba8", 92: "#a6e3a1", 93: "#f9e2af",
    94: "#89b4fa", 95: "#f5c2e7", 96: "#94e2d5", 97: "#ffffff",
}


def resource_path(relative: str) -> str:
    """Absolute path to a bundled resource, inside or outside a frozen build."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


# ── ANSI helpers ──────────────────────────────────────────────────────────


def strip_ansi(text: str) -> str:
    """Drop every ANSI escape sequence."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _parse_ansi_colors(ansi_text: str):
    """Split true-color ANSI text into per-character ``(rgb, char)`` tuples.

    Spaces are kept: they are structural (they hold the picture's alignment),
    so discarding them would shear the artwork sideways.
    """
    lines = []
    for line in ansi_text.split("\n"):
        chars, colors = [], []
        cur = None
        for part in re.split(r"(\033\[[0-9;]*m)", line):
            m = re.match(r"\033\[38;2;(\d+);(\d+);(\d+)m", part)
            if m:
                cur = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif part:
                for ch in part:
                    chars.append(ch)
                    colors.append(cur)
        lines.append((chars, colors))
    return lines


def ansi_to_rice(ansi_text: str, n: int) -> str:
    """Convert true-color ANSI to the ``${cK}`` marker format.

    Colours are quantized into ``n`` palette entries chosen from the image
    itself (k-means over the colours actually used). The result starts with a
    ``(palette c1=R,G,B ...)`` header mapping each ``${cK}`` to its colour.
    """
    import numpy as np

    parsed = _parse_ansi_colors(ansi_text)

    color_set, seen = [], set()
    for chars, colors in parsed:
        for c in colors:
            if c and c not in seen:
                seen.add(c)
                color_set.append(c)

    if not color_set:
        return "(palette )\n"

    k = min(n, len(color_set))
    data = np.asarray(color_set, dtype=np.float32)  # noqa: F841 - kept for clarity

    centroids = list(color_set)
    if len(color_set) > k:
        # Deterministic spread-out seeding, then Lloyd iterations.
        step = len(color_set) / k
        centroids = [color_set[int(i * step)] for i in range(k)]
        for _ in range(15):
            sums = [[0, 0, 0] for _ in range(k)]
            counts = [0] * k
            for c in color_set:
                best, best_d = 0, float("inf")
                for i, cen in enumerate(centroids):
                    d = (c[0] - cen[0]) ** 2 + (c[1] - cen[1]) ** 2 + (c[2] - cen[2]) ** 2
                    if d < best_d:
                        best_d, best = d, i
                sums[best][0] += c[0]
                sums[best][1] += c[1]
                sums[best][2] += c[2]
                counts[best] += 1
            centroids = [
                (
                    int(sums[i][0] / counts[i]),
                    int(sums[i][1] / counts[i]),
                    int(sums[i][2] / counts[i]),
                )
                if counts[i]
                else centroids[i]
                for i in range(k)
            ]

    header = "(palette " + " ".join(
        f"c{i + 1}={c[0]},{c[1]},{c[2]}" for i, c in enumerate(centroids)
    ) + ")"

    def tag_for(color):
        if color is None:
            return None
        best, best_d = 0, float("inf")
        for i, cen in enumerate(centroids):
            d = (color[0] - cen[0]) ** 2 + (color[1] - cen[1]) ** 2 + (color[2] - cen[2]) ** 2
            if d < best_d:
                best_d, best = d, i
        return f"c{best + 1}"

    out = [header]
    for chars, colors in parsed:
        if not chars:
            out.append("")
            continue
        line, cur_tag = "", None
        for ch, col in zip(chars, colors):
            # Spaces carry no colour and need no marker.
            tag = tag_for(col) if (col is not None and ch != " ") else None
            if tag != cur_tag:
                if tag:
                    line += "${" + tag + "}"
                cur_tag = tag
            line += ch
        out.append(line)

    return "\n".join(out)


def truecolor_to_16(ansi_text: str) -> str:
    """Convert 24-bit ANSI to the classic 16-colour ANSI palette."""
    palette = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]

    def closest_index(r, g, b):
        best, best_dist = 0, float("inf")
        for i, (pr, pg, pb) in enumerate(palette):
            d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
            if d < best_dist:
                best_dist, best = d, i
        return best

    def sgr_for_index(idx: int) -> str:
        # 0-7 map to SGR 30-37, 8-15 to the bright range 90-97.
        return str(idx + 30) if idx < 8 else str(idx + 82)

    def replace_match(m):
        codes = m.group(1).split(";")
        i, result = 0, ""
        while i < len(codes):
            c = codes[i]
            if c == "38" and i + 1 < len(codes) and codes[i + 1] == "2" and i + 4 < len(codes):
                idx = closest_index(int(codes[i + 2]), int(codes[i + 3]), int(codes[i + 4]))
                result += f"\033[{sgr_for_index(idx)}m"
                i += 5
            elif c == "38" and i + 1 < len(codes) and codes[i + 1] == "5" and i + 2 < len(codes):
                result += f"\033[38;5;{codes[i + 2]}m"
                i += 3
            else:
                result += f"\033[{c}m"
                i += 1
        return result

    return re.sub(r"\033\[([0-9;]+)m", replace_match, ansi_text)


def _xterm256_hex(n: int) -> str:
    """Hex colour for an xterm-256 palette index."""
    base = [
        "#000000", "#800000", "#008000", "#808000", "#000080", "#800080",
        "#008080", "#c0c0c0", "#808080", "#ff0000", "#00ff00", "#ffff00",
        "#0000ff", "#ff00ff", "#00ffff", "#ffffff",
    ]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        levels = (n // 36, (n // 6) % 6, n % 6)
        comp = [0 if v == 0 else 55 + v * 40 for v in levels]
        return "#{:02x}{:02x}{:02x}".format(*comp)
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _sgr_foreground(codes) -> str | None:
    """Resolve an SGR parameter list to a hex foreground colour, if it sets one."""
    i = 0
    fg = None
    while i < len(codes):
        c = codes[i]
        if c == "38" and i + 1 < len(codes):
            if codes[i + 1] == "2" and i + 4 < len(codes):
                fg = "#{:02x}{:02x}{:02x}".format(
                    int(codes[i + 2]), int(codes[i + 3]), int(codes[i + 4])
                )
                i += 5
                continue
            if codes[i + 1] == "5" and i + 2 < len(codes):
                fg = _xterm256_hex(int(codes[i + 2]))
                i += 3
                continue
        elif c.isdigit():
            n = int(c)
            if n in ANSI_16:
                fg = ANSI_16[n]
            elif n == 0 or n == 39:
                fg = TEXT
        i += 1
    return fg


def ansi_to_ps1_lines(ansi_text: str) -> list[str]:
    """Convert ANSI text to PowerShell ``Write-Host`` lines that keep the colours."""
    ps_lines = []
    for line in ansi_text.split("\n"):
        parts = re.split(r"(\033\[[0-9;]*m)", line)
        segments, fg, buf = [], None, ""

        def flush():
            nonlocal buf
            if buf:
                segments.append((buf, fg))
                buf = ""

        for part in parts:
            m = re.match(r"\033\[(\d+(?:;\d+)*)m", part)
            if m:
                flush()
                fg = _sgr_foreground(m.group(1).split(";"))
            else:
                buf += part
        flush()

        if not segments:
            ps_lines.append('Write-Host ""')
            continue

        calls = []
        for text, color in segments:
            safe = text.replace('"', '`"').replace("$", "`$")
            args = [f'"{safe}"', "-NoNewline"]
            if color:
                args.append(
                    f'-ForegroundColor ([System.Drawing.ColorTranslator]::FromHtml("{color}"))'
                )
            calls.append("Write-Host " + " ".join(args))
        ps_lines.append("; ".join(calls))

    return ps_lines


# ── settings snapshot handed to the worker thread ─────────────────────────


@dataclass(frozen=True)
class RenderSettings:
    width: int
    colors: int
    charset: str
    color_mode: str
    edges: bool
    invert: bool
    fmt: str


class AsciiGUI:
    """The application window."""

    def __init__(self, root, enable_dnd: bool = True):
        self.root = root
        self.image_path: str | None = None
        self.processor = None
        self.ansi_text = ""
        self.rice_text = ""
        self.result_text = ""

        self._debounce_id = None
        self._job_id = 0
        self._thumb_photo = None
        self._last_dir = os.path.expanduser("~")
        self._width = WIDTH_DEFAULT
        self._colors = COLORS_DEFAULT
        self._font_size = 9
        self._drop_targets: list = []
        self._results: queue.Queue = queue.Queue()
        self._dnd = bool(enable_dnd and DND_AVAILABLE)

        self.color_mode = tk.StringVar(value="color")
        self.charset_var = tk.StringVar(value="braille")
        self.edges_var = tk.BooleanVar(value=False)
        self.invert_var = tk.BooleanVar(value=False)
        self.format_var = tk.StringVar(value=FORMAT_LABELS[0])
        self.status_var = tk.StringVar(
            value="Drop an image here, or press Ctrl+O to open one."
        )

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.report_callback_exception = self._report_callback_exception

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._bind_shortcuts()
        self._enable_dnd()
        self.root.after(40, self._drain_results)

    # ── window ────────────────────────────────────────────────────────────

    def _setup_window(self):
        root = self.root
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1180, max(860, sw - 60))
        h = min(820, max(560, sh - 90))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(860, 560)

        icon = resource_path(os.path.join("assets", "icon.ico"))
        if os.path.isfile(icon):
            try:
                root.iconbitmap(default=icon)
            except tk.TclError:
                pass

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=PINK,
                        font=("Segoe UI", 15, "bold"))
        style.configure("Section.TLabel", background=BG, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"))
        style.configure("Field.TLabel", background=BG, foreground=SUBTEXT,
                        font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                        font=("Segoe UI", 9))
        style.configure("Divider.TFrame", background=BORDER)

        style.configure("TButton", background=WIDGET, foreground=TEXT, borderwidth=0,
                        focusthickness=0, padding=(12, 7), font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("pressed", OVERLAY), ("active", BORDER)],
                  foreground=[("disabled", MUTED)])

        style.configure("Accent.TButton", background=ACCENT, foreground=BG, borderwidth=0,
                        focusthickness=0, padding=(14, 7), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_PRESS), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", MUTED)])

        style.configure("Accent.TMenubutton", background=ACCENT, foreground=BG,
                        borderwidth=0, padding=(14, 7), arrowcolor=BG,
                        font=("Segoe UI", 10, "bold"))
        style.map("Accent.TMenubutton",
                  background=[("pressed", ACCENT_PRESS), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", MUTED)])

        style.configure("TCheckbutton", background=BG, foreground=TEXT,
                        font=("Segoe UI", 10), focusthickness=0)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  foreground=[("disabled", MUTED)])

        style.configure("TRadiobutton", background=BG, foreground=TEXT,
                        font=("Segoe UI", 10), focusthickness=0)
        style.map("TRadiobutton",
                  background=[("active", BG)],
                  foreground=[("disabled", MUTED)])

        style.configure("TCombobox", fieldbackground=WIDGET, background=WIDGET,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=WIDGET, darkcolor=WIDGET, padding=(6, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", WIDGET)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", WIDGET)],
                  selectforeground=[("readonly", TEXT)])

        style.configure("TSpinbox", fieldbackground=WIDGET, background=WIDGET,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        insertcolor=TEXT, lightcolor=WIDGET, darkcolor=WIDGET,
                        padding=(4, 3))

        style.configure("TScale", background=ACCENT, troughcolor=WIDGET,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT,
                        gripcount=0)

        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            style.configure(name, background=WIDGET, troughcolor=BG_ALT,
                            bordercolor=BG_ALT, arrowcolor=MUTED,
                            lightcolor=WIDGET, darkcolor=WIDGET)
            style.map(name, background=[("active", OVERLAY)])

        # Clam draws its own indicator colours; set them when they exist.
        for name, opts in (
            ("TCheckbutton", {"indicatorbackground": BG_ALT,
                              "indicatorforeground": ACCENT}),
            ("TRadiobutton", {"indicatorbackground": BG_ALT,
                              "indicatorforeground": ACCENT}),
        ):
            for key, value in opts.items():
                try:
                    style.configure(name, **{key: value})
                except tk.TclError:
                    pass

        # Combobox dropdown lists are plain Tk widgets, not ttk.
        self.root.option_add("*TCombobox*Listbox.background", WIDGET)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # ── layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()

        body = ttk.Frame(self.root)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 8))

        side = ttk.Frame(body, width=286)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        self._build_sidebar(side)

        ttk.Frame(body, style="Divider.TFrame", width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=14
        )

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_preview(right)

        self._build_statusbar()

    def _build_header(self):
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(14, 10))

        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header, text=f"v{APP_VERSION} · image → colored ASCII art",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0), pady=(6, 0))

        ttk.Button(header, text="Open Image…", style="Accent.TButton",
                   command=self._choose_image).pack(side=tk.RIGHT)

    def _section(self, parent, title):
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(wrap, text=title.upper(), style="Section.TLabel").pack(
            anchor=tk.W, pady=(0, 8)
        )
        grid = ttk.Frame(wrap)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)
        return grid

    def _build_sidebar(self, side):
        # ── source ──
        src = self._section(side, "Source")
        self.thumb = tk.Label(
            src, text="Drop an image\nor click Open Image",
            bg=PREVIEW_BG, fg=MUTED, font=("Segoe UI", 9),
            width=30, height=9, bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self.thumb.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self._drop_targets.append(self.thumb)

        self.file_label = ttk.Label(src, text="No image loaded", style="Field.TLabel",
                                    wraplength=270, justify=tk.LEFT)
        self.file_label.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        self.size_label = ttk.Label(src, text="", style="Muted.TLabel")
        self.size_label.grid(row=2, column=0, columnspan=2, sticky=tk.W)

        # ── rendering ──
        grid = self._section(side, "Rendering")

        ttk.Label(grid, text="Charset", style="Field.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        self.charset_box = ttk.Combobox(
            grid, textvariable=self.charset_var, values=list(CHARSET_PRESETS),
            state="readonly", width=12,
        )
        self.charset_box.grid(row=0, column=1, sticky=tk.EW, pady=4)
        self.charset_box.bind("<<ComboboxSelected>>", lambda *_: self._schedule_convert())

        ttk.Label(grid, text="Width", style="Field.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=4
        )
        width_box = ttk.Frame(grid)
        width_box.grid(row=1, column=1, sticky=tk.EW, pady=4)
        self.width_spin = ttk.Spinbox(
            width_box, from_=WIDTH_MIN, to=WIDTH_MAX, width=4,
            justify=tk.CENTER, command=lambda: self._set_width(self.width_spin.get(), "spin"),
        )
        self.width_spin.pack(side=tk.RIGHT)
        self.width_spin.bind("<Return>", lambda *_: self._set_width(self.width_spin.get(), "spin"))
        self.width_spin.bind("<FocusOut>", lambda *_: self._set_width(self.width_spin.get(), "spin"))
        self.width_scale = ttk.Scale(
            width_box, from_=WIDTH_MIN, to=WIDTH_MAX, orient=tk.HORIZONTAL,
            command=lambda v: self._set_width(v, "scale"),
        )
        self.width_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Label(grid, text="Palette", style="Field.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=4
        )
        colors_box = ttk.Frame(grid)
        colors_box.grid(row=2, column=1, sticky=tk.EW, pady=4)
        self.colors_value = ttk.Label(colors_box, text=str(COLORS_DEFAULT),
                                      style="Muted.TLabel", width=3, anchor=tk.E)
        self.colors_value.pack(side=tk.RIGHT)
        self.colors_scale = ttk.Scale(
            colors_box, from_=COLORS_MIN, to=COLORS_MAX, orient=tk.HORIZONTAL,
            command=self._on_colors_scale,
        )
        self.colors_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        toggles = ttk.Frame(grid)
        toggles.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(8, 2))
        ttk.Checkbutton(toggles, text="Edges", variable=self.edges_var,
                        command=self._schedule_convert).pack(side=tk.LEFT)
        ttk.Checkbutton(toggles, text="Invert", variable=self.invert_var,
                        command=self._schedule_convert).pack(side=tk.LEFT, padx=(16, 0))

        ttk.Label(grid, text="Colors", style="Field.TLabel").grid(
            row=4, column=0, sticky=tk.W, pady=4
        )
        modes = ttk.Frame(grid)
        modes.grid(row=4, column=1, sticky=tk.W, pady=4)
        ttk.Radiobutton(modes, text="Full", variable=self.color_mode, value="color",
                        command=self._schedule_convert).pack(side=tk.LEFT)
        ttk.Radiobutton(modes, text="B&W", variable=self.color_mode, value="bw",
                        command=self._schedule_convert).pack(side=tk.LEFT, padx=(12, 0))

        # ── output ──
        out = self._section(side, "Output format")
        self.format_box = ttk.Combobox(
            out, textvariable=self.format_var, values=FORMAT_LABELS,
            state="readonly", width=24,
        )
        self.format_box.grid(row=0, column=0, columnspan=2, sticky=tk.EW)
        self.format_box.bind("<<ComboboxSelected>>", lambda *_: self._on_format_change())
        self.format_hint = ttk.Label(out, text="", style="Muted.TLabel",
                                     wraplength=270, justify=tk.LEFT)
        self.format_hint.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        # ── defaults ──
        ttk.Button(side, text="Reset to defaults", command=self._reset).pack(
            fill=tk.X, pady=(2, 0)
        )

        self._set_width(WIDTH_DEFAULT, "init")
        self.colors_scale.set(COLORS_DEFAULT)
        self._update_format_hint()

    def _build_preview(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(bar, text="PREVIEW", style="Section.TLabel").pack(side=tk.LEFT)

        self.zoom_in = ttk.Button(bar, text="+", width=3, command=lambda: self._zoom(1))
        self.zoom_in.pack(side=tk.RIGHT)
        self.zoom_label = ttk.Label(bar, text=str(self._font_size), style="Muted.TLabel",
                                    width=3, anchor=tk.CENTER)
        self.zoom_label.pack(side=tk.RIGHT)
        self.zoom_out = ttk.Button(bar, text="–", width=3, command=lambda: self._zoom(-1))
        self.zoom_out.pack(side=tk.RIGHT)

        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.preview_font = (
            self._pick_font(),
            self._font_size,
        )
        self.preview = tk.Text(
            wrap, bg=PREVIEW_BG, fg=TEXT, font=self.preview_font, wrap=tk.NONE,
            insertbackground=TEXT, selectbackground=OVERLAY, selectforeground=TEXT,
            bd=0, highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=BORDER, padx=10, pady=8, state=tk.DISABLED,
        )
        self.preview.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.preview.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.preview.xview)
        xscroll.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.preview.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self._drop_targets.append(self.preview)

        actions = ttk.Frame(parent)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(actions, text="Copy", command=self._copy).pack(side=tk.LEFT)

        save = ttk.Menubutton(actions, text="Save", style="Accent.TMenubutton")
        menu = tk.Menu(save, tearoff=False, bg=WIDGET, fg=TEXT, bd=0,
                       activebackground=ACCENT, activeforeground=BG,
                       activeborderwidth=0)
        menu.add_command(label="Text file  (.txt)", command=self._save_txt)
        menu.add_command(label="PowerShell script  (.ps1)", command=self._save_ps1)
        menu.add_command(label="CMD script  (.cmd)", command=self._save_cmd)
        save["menu"] = menu
        save.pack(side=tk.LEFT, padx=(8, 0))

    def _build_statusbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Frame(bar, style="Divider.TFrame", height=1).pack(fill=tk.X)
        inner = ttk.Frame(bar)
        inner.pack(fill=tk.X, padx=16, pady=(6, 8))
        ttk.Label(inner, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.LEFT)
        hint = "Ctrl+O open · Ctrl+S save · Ctrl+Shift+C copy · F5 redraw"
        ttk.Label(inner, text=hint, style="Muted.TLabel").pack(side=tk.RIGHT)

    def _pick_font(self) -> str:
        try:
            families = set(tkfont.families(self.root))
        except tk.TclError:
            return "Courier New"
        for name in PREVIEW_FONT_CANDIDATES:
            if name in families:
                return name
        return "Courier New"

    # ── widget plumbing ───────────────────────────────────────────────────

    def _set_width(self, value, source):
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            value = self._width
        value = max(WIDTH_MIN, min(WIDTH_MAX, value))

        changed = value != self._width
        self._width = value
        try:
            if source != "scale":
                self.width_scale.set(value)
            if source != "spin":
                self.width_spin.set(value)
        except (tk.TclError, AttributeError):
            pass
        if changed and source != "init":
            self._schedule_convert()

    def _on_colors_scale(self, value):
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            return
        if n == self._colors:
            return
        self._colors = n
        self.colors_value.configure(text=str(n))
        self._schedule_convert()

    def _zoom(self, delta):
        self._font_size = max(FONT_MIN, min(FONT_MAX, self._font_size + delta))
        self.zoom_label.configure(text=str(self._font_size))
        self.preview.configure(font=(self.preview_font[0], self._font_size))

    def _update_format_hint(self):
        hints = {
            "rice": "Quantized palette with ${cN} markers — portable, no ANSI.",
            "ansi_truecolor": "Raw 24-bit ANSI. Needs a modern true-color terminal.",
            "ansi_16": "Classic 16-color ANSI. Works in old cmd.exe / conhost.",
            "plain": "Monochrome text with all ANSI styling removed.",
        }
        self.format_hint.configure(text=hints.get(self._format_key(), ""))
        state = tk.NORMAL if self._format_key() == "rice" else tk.DISABLED
        self.colors_scale.configure(state=state)
        self.colors_value.configure(state=state)

    def _format_key(self) -> str:
        return FORMAT_BY_LABEL.get(self.format_var.get(), "rice")

    def _on_format_change(self):
        self._update_format_hint()
        self._schedule_convert()

    def _snapshot(self) -> RenderSettings:
        """Read every setting on the main thread; the worker never touches Tk."""
        return RenderSettings(
            width=self._width,
            colors=self._colors,
            charset=CHARSET_PRESETS.get(self.charset_var.get(), DEFAULT_CHARSET),
            color_mode=self.color_mode.get(),
            edges=bool(self.edges_var.get()),
            invert=bool(self.invert_var.get()),
            fmt=self._format_key(),
        )

    def _report_callback_exception(self, exc, value, tb):
        self._set_status(f"Error: {value}")
        messagebox.showerror(APP_NAME, f"Unexpected error:\n\n{value}")

    # ── shortcuts & drag and drop ─────────────────────────────────────────

    def _bind_shortcuts(self):
        self.root.bind("<Control-o>", lambda *_: self._choose_image())
        self.root.bind("<Control-s>", lambda *_: self._save_txt())
        self.root.bind("<Control-Shift-KeyPress-C>", lambda *_: self._copy())
        self.root.bind("<F5>", lambda *_: self._convert())
        self.root.bind("<Control-plus>", lambda *_: self._zoom(1))
        self.root.bind("<Control-equal>", lambda *_: self._zoom(1))
        self.root.bind("<Control-minus>", lambda *_: self._zoom(-1))
        self.root.bind("<Control-0>", lambda *_: self._zoom(-9))
        self.preview.bind("<Control-MouseWheel>", self._on_ctrl_wheel)

    def _on_ctrl_wheel(self, event):
        self._zoom(1 if event.delta > 0 else -1)
        return "break"

    def _enable_dnd(self):
        if not self._dnd:
            return
        for widget in [self.root, *self._drop_targets]:
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DragEnter>>", lambda e: self._highlight_drop(True))
                widget.dnd_bind("<<DragLeave>>", lambda e: self._highlight_drop(False))
            except Exception:
                continue

    def _highlight_drop(self, active):
        try:
            self.thumb.configure(highlightbackground=ACCENT if active else BORDER)
        except tk.TclError:
            pass

    def _on_drop(self, event):
        self._highlight_drop(False)
        try:
            items = self.root.tk.splitlist(event.data)
        except Exception:
            items = [event.data]
        for item in items:
            if os.path.isfile(item):
                self.load_image(item)
                return
        self._set_status("Dropped item is not a file.")

    # ── loading ───────────────────────────────────────────────────────────

    def _choose_image(self):
        path = filedialog.askopenfilename(
            title="Open image", filetypes=IMAGE_FILETYPES, initialdir=self._last_dir
        )
        if path:
            self.load_image(path)

    def load_image(self, path):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            messagebox.showerror(APP_NAME, f"No such file:\n{path}")
            return
        try:
            processor = ImgProcessor(path)
            if processor.image is None:
                raise ValueError("Unsupported or unreadable image format.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open that image.\n\n{exc}")
            self._set_status("Failed to load image.")
            return

        self.image_path = path
        self.processor = processor
        self._last_dir = os.path.dirname(path)

        rows, cols = processor.image.shape[:2]
        self.file_label.configure(text=os.path.basename(path))
        self.size_label.configure(text=f"{cols} × {rows} px")
        self._show_thumbnail(path)
        self._convert()

    def _show_thumbnail(self, path):
        try:
            from PIL import Image as PILImage, ImageTk

            img = PILImage.open(path)
            img.thumbnail(THUMB_MAX)
            self._thumb_photo = ImageTk.PhotoImage(img)
            self.thumb.configure(image=self._thumb_photo, text="", width=img.width,
                                 height=img.height)
        except Exception:
            self._thumb_photo = None
            self.thumb.configure(image="", text="(no preview)", width=30, height=9)

    # ── conversion ────────────────────────────────────────────────────────

    def _schedule_convert(self):
        # Bump immediately: any in-flight render is now stale.
        self._job_id += 1
        if self._debounce_id is not None:
            self.root.after_cancel(self._debounce_id)
        self._debounce_id = self.root.after(DEBOUNCE_MS, self._convert)

    def _convert(self):
        self._debounce_id = None
        if self.processor is None:
            return
        self._job_id += 1
        job = self._job_id
        self._set_status("Converting…")
        threading.Thread(
            target=self._worker, args=(job, self._snapshot()), daemon=True
        ).start()

    def _worker(self, job, cfg: RenderSettings):
        started = time.perf_counter()
        try:
            proc = self.processor
            rows, cols = proc.image.shape[:2]
            height = max(1, int(round(cfg.width * rows / cols / 2)))

            ds_factor = proc.calculate_downsample_factor(
                term_height=height, term_width=cfg.width,
                keep_aspect_ratio=True, f_type="in_terminal",
            )
            ds_img = proc.downsample_image(f=ds_factor, keep_aspect_ratio=True)
            if cfg.invert:
                ds_img = 255 - ds_img
            img_hsv = proc.convert_to_hsv(image=ds_img)

            edges = angles = None
            if cfg.edges:
                angles = proc.calculate_angles(image=ds_img)
                edges = proc.detect_edges(image=ds_img)

            renderer = Renderer(color_mode=cfg.color_mode, charset=cfg.charset)
            if edges is not None:
                ansi = renderer.draw_in_ascii_with_edges(
                    img_hsv=img_hsv, angles=angles, edges=edges
                )
            else:
                ansi = renderer.draw_in_ascii(img_hsv=img_hsv)

            rice_text = ""
            if cfg.fmt == "rice":
                display = ansi_to_rice(ansi, cfg.colors)
                rice_text = display
            elif cfg.fmt == "ansi_16":
                display = truecolor_to_16(ansi)
            elif cfg.fmt == "ansi_truecolor":
                display = ansi
            else:
                display = strip_ansi(ansi)
        except Exception as exc:
            self._results.put((job, "err", exc))
            return

        self._results.put(
            (job, "ok", (ansi, display, rice_text, cfg, time.perf_counter() - started))
        )

    def _drain_results(self):
        """Apply worker results on the main thread — the worker never touches Tk."""
        try:
            while True:
                job, kind, payload = self._results.get_nowait()
                if job != self._job_id:
                    continue  # a newer render is already on its way
                if kind == "err":
                    self._on_failed(payload)
                else:
                    self._on_rendered(*payload)
        except queue.Empty:
            pass
        self.root.after(40, self._drain_results)

    def _on_failed(self, exc):
        self._set_status(f"Error: {exc}")
        messagebox.showerror(APP_NAME, f"Conversion failed.\n\n{exc}")

    def _on_rendered(self, ansi, display, rice_text, cfg, elapsed):
        self.ansi_text = ansi
        self.rice_text = rice_text
        self.result_text = display
        self._render_preview(display, rice_text, cfg.fmt)
        lines = display.count("\n") + 1
        self._set_status(
            f"{lines} lines · {cfg.width} cols · {os.path.basename(self.image_path)} · {elapsed:.2f} s"
        )

    def _render_preview(self, display, rice_text, fmt):
        self.preview.configure(state=tk.NORMAL)
        self.preview.delete("1.0", tk.END)
        try:
            if fmt == "rice" and rice_text:
                self._insert_rice(rice_text)
            elif fmt in ("ansi_truecolor", "ansi_16"):
                source = self.ansi_text if fmt == "ansi_truecolor" else truecolor_to_16(self.ansi_text)
                self._insert_ansi(source)
            else:
                self.preview.insert("1.0", display)
        finally:
            self.preview.configure(state=tk.DISABLED)
        self.preview.yview_moveto(0.0)
        self.preview.xview_moveto(0.0)

    def _insert_rice(self, text):
        """Render the ``${cK}`` format using the colours from its palette header."""
        palette = {}
        header_seen = False
        for line in text.split("\n"):
            if not header_seen and line.startswith("(palette "):
                m = re.match(r"^\(palette\s+(.+)\)$", line)
                if m:
                    for token in m.group(1).split():
                        if "=" in token:
                            key, rgb = token.split("=", 1)
                            parts = rgb.split(",")
                            if len(parts) == 3:
                                palette[key] = "#{:02x}{:02x}{:02x}".format(
                                    int(parts[0]), int(parts[1]), int(parts[2])
                                )
                header_seen = True
                continue

            cur_tag = None
            for part in re.split(r"(\$\{[^}]+\})", line):
                m = re.match(r"^\$\{(c\d+)\}$", part)
                if m:
                    cur_tag = m.group(1)
                elif part:
                    tag = f"rice_{cur_tag or 'default'}"
                    self.preview.tag_configure(tag, foreground=palette.get(cur_tag, TEXT))
                    self.preview.insert(tk.END, part, tag)
            self.preview.insert(tk.END, "\n")

    def _insert_ansi(self, text):
        """Insert ANSI text, caching one tag per distinct colour."""
        tag_cache = {}
        fg = TEXT
        for part in re.split(r"(\033\[[0-9;]*m)", text):
            m = re.match(r"\033\[(\d+(?:;\d+)*)m", part)
            if m:
                resolved = _sgr_foreground(m.group(1).split(";"))
                if resolved:
                    fg = resolved
            elif part:
                tag = tag_cache.get(fg)
                if tag is None:
                    tag = f"ansi_{len(tag_cache)}"
                    self.preview.tag_configure(tag, foreground=fg)
                    tag_cache[fg] = tag
                self.preview.insert(tk.END, part, tag)

    def _set_status(self, message):
        self.status_var.set(message)

    # ── actions ───────────────────────────────────────────────────────────

    def _reset(self):
        self._set_width(WIDTH_DEFAULT, "init")
        self.colors_scale.set(COLORS_DEFAULT)
        self._on_colors_scale(str(COLORS_DEFAULT))
        self.charset_var.set("braille")
        self.color_mode.set("color")
        self.edges_var.set(False)
        self.invert_var.set(False)
        self.format_var.set(FORMAT_LABELS[0])
        self._font_size = 9
        self.zoom_label.configure(text="9")
        self.preview.configure(font=(self.preview_font[0], self._font_size))
        self._update_format_hint()
        self._schedule_convert()

    def _copy(self):
        if not self.result_text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.result_text)
        self._set_status("Copied to clipboard.")

    def _ask_save_path(self, defext, filetypes):
        return filedialog.asksaveasfilename(
            defaultextension=defext, filetypes=filetypes, initialdir=self._last_dir
        )

    def _save_txt(self):
        if not self.result_text:
            return
        path = self._ask_save_path(".txt", [("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        data = self.rice_text if (self._format_key() == "rice" and self.rice_text) else self.result_text
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the file.\n\n{exc}")
            return
        self._set_status(f"Saved {os.path.basename(path)}")

    def _save_ps1(self):
        if not self.ansi_text:
            return
        path = self._ask_save_path(
            ".ps1", [("PowerShell script", "*.ps1"), ("All files", "*.*")]
        )
        if not path:
            return
        header = [
            "# ASCII art — generated by asciify-ps",
            "# Run it in PowerShell 7+ or Windows Terminal for true colors.",
            "",
        ]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(header + ansi_to_ps1_lines(self.ansi_text)) + "\n")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the file.\n\n{exc}")
            return
        self._set_status(f"Saved {os.path.basename(path)}")

    def _save_cmd(self):
        if not self.ansi_text:
            return
        path = self._ask_save_path(
            ".cmd", [("CMD script", "*.cmd *.bat"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("@echo off\n")
                fh.write("REM ASCII art — generated by asciify-ps\n")
                fh.write("REM Needs Windows Terminal or a modern console.\n\n")
                for line in self.ansi_text.split("\n"):
                    safe = line.replace("%", "%%").replace("!", "^!")
                    fh.write(f"echo {safe}\n")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the file.\n\n{exc}")
            return
        self._set_status(f"Saved {os.path.basename(path)}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if LIB_ERROR is not None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            "The asciify rendering library could not be imported.\n\n"
            f"{LIB_ERROR}\n\nInstall the requirements with:\n"
            "    pip install -r requirements.txt",
        )
        return 1

    # tkinterdnd2 can import yet still fail to load its Tcl extension; in that
    # case fall back to a plain root rather than refusing to start.
    root, dnd_ready = None, False
    if DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()
            dnd_ready = True
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()

    app = AsciiGUI(root, enable_dnd=dnd_ready)

    initial = next((a for a in argv if not a.startswith("-")), None)
    if initial and os.path.isfile(initial):
        root.after(60, lambda: app.load_image(initial))

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
