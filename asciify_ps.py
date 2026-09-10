#!/usr/bin/env python3
"""
asciify-ps — Image to ASCII art generator with live preview.
"""

import os
import sys
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

ASCIIFY_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asciify-them", "src")
if ASCIIFY_SRC not in sys.path:
    sys.path.insert(0, ASCIIFY_SRC)

from asciify.core import asciify as asciify_raw
from asciify.process import ImgProcessor
from asciify.renderer import Renderer
from asciify.utils import DEFAULT_CHARSET, CHARSET_PRESETS


def strip_ansi(text):
    return re.sub(r'\033\[[0-9;]*m', '', text)


def _parse_ansi_colors(ansi_text):
    """Split ANSI true-color text into per-character (rgb, char) tuples, line by line."""
    lines = []
    for line in ansi_text.split("\n"):
        chars, colors = [], []
        cur = None
        for part in re.split(r'(\033\[[0-9;]*m)', line):
            m = re.match(r'\033\[38;2;(\d+);(\d+);(\d+)m', part)
            if m:
                cur = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif part:
                for ch in part:
                    if ch == ' ':
                        continue  # skip background
                    chars.append(ch)
                    colors.append(cur)
        lines.append((chars, colors))
    return lines


def ansi_to_rice(ansi_text, n):
    """Convert true-color ANSI to the rice ${cK} marker format.

    Colors are quantized into `n` distinct palette colors chosen from the image
    itself (k-means on the actual pixel colors). Output starts with a
    `(palette ...)` header line mapping each ${cK} to its R,G,B value.
    """
    import numpy as np
    parsed = _parse_ansi_colors(ansi_text)

    # Collect all distinct colors used
    color_set = []
    seen = set()
    for chars, colors in parsed:
        for c in colors:
            if c and c not in seen:
                seen.add(c)
                color_set.append(c)

    if not color_set:
        return "(palette )\n"

    # K-means: reduce to n clusters
    k = min(n, len(color_set))
    data = np.array(color_set, dtype=np.float32)
    # Simple k-means++-ish initialization by sampling; then Lloyd iterations.
    centroids = {c: c for c in color_set}  # start with each distinct color
    # fallback: uniform sampling if too many
    if len(color_set) > k:
        # pick k spread-out seeds
        from random import Random
        rnd = Random(42)
        step = len(color_set) / k
        seeds = [color_set[int(i * step)] for i in range(k)]
        centroids = seeds
        # assign + iterate
        assignments = {}
        for _ in range(15):
            sums = {i: [0, 0, 0] for i in range(k)}
            counts = {i: 0 for i in range(k)}
            for c in color_set:
                best, best_d = 0, float('inf')
                for i, cen in enumerate(centroids):
                    d = (c[0]-cen[0])**2 + (c[1]-cen[1])**2 + (c[2]-cen[2])**2
                    if d < best_d:
                        best_d, best = d, i
                sums[best][0] += c[0]; sums[best][1] += c[1]; sums[best][2] += c[2]
                counts[best] += 1
                assignments[c] = best
            new_c = []
            for i in range(k):
                if counts[i]:
                    new_c.append((int(sums[i][0]/counts[i]), int(sums[i][1]/counts[i]), int(sums[i][2]/counts[i])))
                else:
                    new_c.append(centroids[i])
            centroids = new_c
    else:
        assignments = {color_set[i]: i for i in range(len(color_set))}

    # Build palette header
    palette_lines = []
    mapped = {}
    for i, cen in enumerate(centroids):
        tag = f"c{i+1}"
        mapped[cen] = tag
        palette_lines.append(f"c{i+1}={cen[0]},{cen[1]},{cen[2]}")

    header = "(palette " + " ".join(palette_lines) + ")"

    def tag_for(color):
        if color is None:
            return None
        if color in mapped:
            return mapped[color]
        # nearest centroid
        best, best_d = 0, float('inf')
        for i, cen in enumerate(centroids):
            d = (color[0]-cen[0])**2 + (color[1]-cen[1])**2 + (color[2]-cen[2])**2
            if d < best_d:
                best_d, best = d, i
        return f"c{best+1}"

    # Build output lines with ${cK} markers before color runs
    out = [header]
    for chars, colors in parsed:
        if not chars:
            out.append("")
            continue
        line = ""
        cur_tag = None
        for ch, col in zip(chars, colors):
            tag = tag_for(col) if col else None
            if tag != cur_tag:
                if tag:
                    line += "${" + tag + "}"
                cur_tag = tag
            line += ch
        out.append(line)

    return "\n".join(out)


def truecolor_to_16(ansi_text):
    """Convert true-color ANSI to 16-color ANSI (works in cmd.exe and old terminals)."""
    # Standard 16-color palette: index → RGB
    palette = [
        (0,0,0), (128,0,0), (0,128,0), (128,128,0),
        (0,0,128), (128,0,128), (0,128,128), (192,192,192),
        (128,128,128), (255,0,0), (0,255,0), (255,255,0),
        (0,0,255), (255,0,255), (0,255,255), (255,255,255),
    ]

    def closest_color(r, g, b):
        best, best_dist = 7, float('inf')
        for i, (pr, pg, pb) in enumerate(palette):
            d = (r-pr)**2 + (g-pg)**2 + (b-pb)**2
            if d < best_dist:
                best_dist = d
                best = i
        # Map to ANSI code: 0-7 are normal, 8-15 are bright
        return str(best) if best < 8 else str(best)

    def replace_match(m):
        codes = m.group(1).split(";")
        i = 0
        result = ""
        while i < len(codes):
            c = codes[i]
            if c == "38" and i+1 < len(codes) and codes[i+1] == "2" and i+4 < len(codes):
                r, g, b = int(codes[i+2]), int(codes[i+3]), int(codes[i+4])
                idx = closest_color(r, g, b)
                result += f"\033[{idx}m"
                i += 5
            elif c == "38" and i+1 < len(codes) and codes[i+1] == "5":
                result += f"\033[38;5;{codes[i+2]}m"
                i += 3
            else:
                result += f"\033[{c}m"
                i += 1
        return result

    return re.sub(r'\033\[([0-9;]+)m', replace_match, ansi_text)


def ansi_to_ps1_lines(ansi_text):
    """Convert ANSI text to PowerShell lines using Write-Host with colors."""
    lines = ansi_text.split("\n")
    ps_lines = []
    for line in lines:
        # Parse ANSI codes and build Write-Host command
        parts = re.split(r'(\033\[[0-9;]*m)', line)
        cmd_parts = []
        current_fg = None
        current_bg = None
        buf = ""

        for part in parts:
            m = re.match(r'\033\[(\d+(?:;\d+)*)m', part)
            if m:
                if buf:
                    cmd_parts.append((buf, current_fg, current_bg))
                    buf = ""
                codes = m.group(1).split(";")
                i = 0
                while i < len(codes):
                    c = codes[i]
                    if c == "38" and i+1 < len(codes) and codes[i+1] == "2" and i+4 < len(codes):
                        current_fg = f"#{int(codes[i+2]):02x}{int(codes[i+3]):02x}{int(codes[i+4]):02x}"
                        i += 5
                    elif c == "48" and i+1 < len(codes) and codes[i+1] == "2" and i+4 < len(codes):
                        current_bg = f"#{int(codes[i+2]):02x}{int(codes[i+3]):02x}{int(codes[i+4]):02x}"
                        i += 5
                    else:
                        i += 1
            else:
                buf += part

        if buf:
            cmd_parts.append((buf, current_fg, current_bg))

        # Build Write-Host line
        if not cmd_parts:
            ps_lines.append('Write-Host ""')
        else:
            segments = []
            for text, fg, bg in cmd_parts:
                safe = text.replace('"', '`"').replace("$", "`$")
                args = [f'"{safe}"', "-NoNewline"]
                if fg:
                    args.append(f'-ForegroundColor ([System.Drawing.ColorTranslator]::FromHtml("{fg}"))')
                if bg:
                    args.append(f'-BackgroundColor ([System.Drawing.ColorTranslator]::FromHtml("{bg}"))')
                segments.append("Write-Host " + " ".join(args))
            ps_lines.append("; ".join(segments))

    return ps_lines


def ansi_to_cmd_lines(ansi_text):
    """Return raw ANSI text — Windows Terminal and modern cmd.exe handle it natively."""
    return [ansi_text]


class AsciiGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("asciify-ps")
        self.root.geometry("1100x800")
        self.root.configure(bg="#1e1e2e")
        self.root.minsize(700, 500)

        self.image_path = None
        self.processor = None  # cached ImgProcessor
        self.ansi_text = ""
        self.result_text = ""
        self._debounce_id = None
        self._thumb_photo = None

        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        bg = "#1e1e2e"
        fg = "#cdd6f4"
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=bg, foreground="#f5c2e7", font=("Segoe UI", 14, "bold"))
        style.configure("Accent.TButton", background="#89b4fa", foreground="#1e1e2e", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", background="#313244", foreground=fg, font=("Segoe UI", 10))
        style.configure("TCheckbutton", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TRadiobutton", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TScale", background=bg, troughcolor="#313244")

        # ── Header row ──
        hdr = ttk.Frame(self.root)
        hdr.pack(fill=tk.X, padx=10, pady=(10, 5))
        ttk.Label(hdr, text="asciify-ps", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(hdr, text="📂 Open", style="Accent.TButton",
                   command=self._load_image).pack(side=tk.RIGHT, padx=5)

        # ── Image preview on TOP ──
        self.lbl_thumb = ttk.Label(self.root, anchor=tk.CENTER)
        self.lbl_thumb.pack(fill=tk.X, padx=10, pady=(0, 5))
        self.lbl_thumb_configure_done = True

        # ── Controls bar ──
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill=tk.X, padx=10, pady=5)

        # Width: slider + editable entry
        ttk.Label(ctrl, text="WIDTH").grid(row=0, column=0, padx=(0, 5))
        self.width_var = tk.IntVar(value=100)
        self.width_scale = ttk.Scale(ctrl, from_=20, to=300, variable=self.width_var,
                                      orient=tk.HORIZONTAL, length=200)
        self.width_scale.grid(row=0, column=1, padx=5)
        self.width_entry = ttk.Entry(ctrl, textvariable=self.width_var, width=5, justify=tk.CENTER)
        self.width_entry.grid(row=0, column=2, padx=(0, 15))
        # Sync entry → slider
        self.width_var.trace_add("write", self._on_width_entry_change)

        # Color
        ttk.Label(ctrl, text="Color").grid(row=0, column=3, padx=(10, 0))
        self.color_mode = tk.StringVar(value="color")
        ttk.Radiobutton(ctrl, text="Yes", variable=self.color_mode, value="color",
                        command=self._schedule_convert).grid(row=0, column=4)
        ttk.Radiobutton(ctrl, text="B&W", variable=self.color_mode, value="bw",
                        command=self._schedule_convert).grid(row=0, column=5)

        # Checkboxes
        self.edges_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Edges", variable=self.edges_var,
                        command=self._schedule_convert).grid(row=0, column=6, padx=(15, 0))
        self.invert_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Invert", variable=self.invert_var,
                        command=self._schedule_convert).grid(row=0, column=7, padx=(10, 0))

        # Charset
        ttk.Label(ctrl, text="Charset").grid(row=1, column=0, padx=(0, 5), pady=5)
        self.charset_var = tk.StringVar(value="braille")
        self.charset_menu = ttk.Combobox(ctrl, textvariable=self.charset_var,
                                          values=list(CHARSET_PRESETS.keys()),
                                          state="readonly", width=15)
        self.charset_menu.grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=5)
        self.charset_var.trace_add("write", self._schedule_convert)

        # Output format
        ttk.Label(ctrl, text="Output").grid(row=1, column=3, padx=(10, 0))
        self.output_fmt = tk.StringVar(value="rice")
        ttk.Radiobutton(ctrl, text="Rice {cN}", variable=self.output_fmt,
                        value="rice", command=self._schedule_convert).grid(row=1, column=4)
        ttk.Radiobutton(ctrl, text="True Color", variable=self.output_fmt,
                        value="ansi_truecolor", command=self._schedule_convert).grid(row=1, column=5)
        ttk.Radiobutton(ctrl, text="Plain", variable=self.output_fmt,
                        value="plain", command=self._schedule_convert).grid(row=1, column=6)

        # Color count (for Rice output)
        ttk.Label(ctrl, text="Colors").grid(row=0, column=8, padx=(15, 0))
        self.colors_var = tk.IntVar(value=8)
        self.colors_scale = ttk.Scale(ctrl, from_=2, to=24, variable=self.colors_var,
                                       orient=tk.HORIZONTAL, length=120,
                                       command=lambda *a: (self.lbl_colors.config(text=str(self.colors_var.get())),
                                                           self._schedule_convert()))
        self.colors_scale.grid(row=0, column=9)
        self.lbl_colors = ttk.Label(ctrl, text="8", width=3)
        self.lbl_colors.grid(row=0, column=10, padx=5)

        # ── Buttons ──
        btn = ttk.Frame(self.root)
        btn.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Button(btn, text="📋 Copy", command=self._copy).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn, text="💾 Save .txt", command=self._save_txt).pack(side=tk.LEFT, padx=2)

        # ── Preview ──
        pf = ttk.Frame(self.root)
        pf.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))
        self.preview = tk.Text(pf, bg="#11111b", fg="#cdd6f4",
                               font=("Consolas", 9), wrap=tk.NONE,
                               insertbackground="#cdd6f4", selectbackground="#45475a",
                               borderwidth=0, highlightthickness=0)
        self.preview.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        ys = ttk.Scrollbar(pf, orient=tk.VERTICAL, command=self.preview.yview)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview.configure(yscrollcommand=ys.set)
        xs = ttk.Scrollbar(self.root, orient=tk.HORIZONTAL, command=self.preview.xview)
        xs.pack(fill=tk.X, padx=10, pady=(0, 5))
        self.preview.configure(xscrollcommand=xs.set)

        # ── Status ──
        self.status_var = tk.StringVar(value="Load an image to start")
        ttk.Label(self.root, textvariable=self.status_var).pack(padx=10, pady=(0, 10))

    # ── Width entry sync ───────────────────────────────────────────────────

    def _on_width_entry_change(self, *a):
        try:
            v = self.width_var.get()
            if 20 <= v <= 300:
                # Update slider position without triggering convert
                self.width_scale.set(v)
                self._schedule_convert()
        except tk.TclError:
            pass

    # ── Live update ────────────────────────────────────────────────────────

    def _schedule_convert(self, *a):
        if self._debounce_id:
            self.root.after_cancel(self._debounce_id)
        self._debounce_id = self.root.after(350, self._convert)

    def _load_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"), ("All", "*.*")]
        )
        if not path:
            return
        self.image_path = path

        # Show thumbnail on top
        try:
            from PIL import Image as PILImage, ImageTk
            img = PILImage.open(path)
            img.thumbnail((160, 160))
            self._thumb_photo = ImageTk.PhotoImage(img)
            self.lbl_thumb.configure(image=self._thumb_photo, text="")
        except Exception:
            self.lbl_thumb.configure(image="", text=os.path.basename(path))

        # Cache the processor so we don't re-load the image every time
        self.processor = ImgProcessor(path)
        self._convert()

    def _get_charset(self):
        name = self.charset_var.get()
        return CHARSET_PRESETS.get(name, DEFAULT_CHARSET)

    def _convert(self):
        if not self.image_path or not self.processor:
            return

        self.status_var.set("Converting…")
        self.root.update_idletasks()

        def run():
            try:
                proc = self.processor
                width = self.width_var.get()
                charset = self._get_charset()

                # Compute size from cached processor (no re-load)
                m, n, _ = proc.image.shape
                height = max(1, int(width * m / n / 2))

                # Handle invert
                image = proc.image.copy()
                if self.invert_var.get():
                    import numpy as np
                    image = 255 - image

                # Downsample
                ds_f = proc.calculate_downsample_factor(
                    term_height=height, term_width=width,
                    keep_aspect_ratio=True, f_type="in_terminal"
                )
                ds_img = proc.downsample_image(f=ds_f, keep_aspect_ratio=True)
                img_hsv = proc.convert_to_hsv(image=ds_img)

                # Edge detection only if enabled
                edges = None
                angles = None
                if self.edges_var.get():
                    angles = proc.calculate_angles(image=ds_img)
                    edges = proc.detect_edges(image=ds_img)

                # Render
                renderer = Renderer(color_mode=self.color_mode.get(), charset=charset)
                if edges is not None:
                    ansi = renderer.draw_in_ascii_with_edges(img_hsv=img_hsv, angles=angles, edges=edges)
                else:
                    ansi = renderer.draw_in_ascii(img_hsv=img_hsv)

                self.ansi_text = ansi

                fmt = self.output_fmt.get()
                if fmt == "rice":
                    display = ansi_to_rice(ansi, self.colors_var.get())
                    self.rice_text = display
                elif fmt == "ansi_truecolor":
                    display = ansi
                elif fmt == "ansi_16":
                    display = truecolor_to_16(ansi)
                else:
                    display = strip_ansi(ansi)

                self.result_text = display
                self.root.after(0, self._show_preview, display)

            except Exception as e:
                self.root.after(0, lambda: self.status_var.set(f"Error: {e}"))

        threading.Thread(target=run, daemon=True).start()

    def _show_preview(self, display_text):
        self.preview.delete("1.0", tk.END)

        fmt = self.output_fmt.get()
        if fmt == "rice" and getattr(self, 'rice_text', None):
            self._insert_rice_colored(self.rice_text)
        elif fmt in ("ansi_truecolor", "ansi_16") and self.ansi_text:
            source = self.ansi_text if fmt == "ansi_truecolor" else truecolor_to_16(self.ansi_text)
            self._insert_colored(source)
        else:
            self.preview.insert("1.0", display_text)

        lines = display_text.count("\n") + 1
        self.status_var.set(f"{lines} lines")

    def _insert_rice_colored(self, text):
        """Render rice ${cK} format with palette colors from the header line."""
        # Parse palette from header:  (palette c1=R,G,B c2=R,G,B ...)
        palette = {}
        header_consumed = False
        self.preview.delete("1.0", tk.END)

        for line in text.split("\n"):
            if not header_consumed and line.startswith("(palette "):
                m = re.match(r'^\(palette\s+(.+)\)$', line)
                if m:
                    for tok in m.group(1).split():
                        if '=' in tok:
                            k, rgb = tok.split('=', 1)
                            parts = rgb.split(',')
                            if len(parts) == 3:
                                palette[k] = f"#{int(parts[0]):02x}{int(parts[1]):02x}{int(parts[2]):02x}"
                header_consumed = True
                # We don't print the header line to the preview
                continue
            # Render the line with ${cK} markers
            cur_tag = None
            for part in re.split(r'(\$\{[^}]+\})', line):
                m = re.match(r'^\$\{(c\d+)\}$', part)
                if m:
                    cur_tag = m.group(1)
                elif part:
                    fg = palette.get(cur_tag, "#cdd6f4")
                    tag = f"r_{cur_tag or 'd'}"
                    self.preview.tag_configure(tag, foreground=fg)
                    self.preview.insert(tk.END, part, tag)
            self.preview.insert(tk.END, "\n")

    def _insert_colored(self, text):
        """Insert ANSI text with quantized color tags for performance."""
        self.preview.delete("1.0", tk.END)
        parts = re.split(r'(\033\[[0-9;]*m)', text)
        fg = "#cdd6f4"

        # Quantize colors to 6-bit (64 unique colors) for fewer tags
        tag_cache = {}

        for part in parts:
            m = re.match(r'\033\[(\d+(?:;\d+)*)m', part)
            if m:
                codes = m.group(1).split(";")
                i = 0
                while i < len(codes):
                    c = codes[i]
                    if c == "38" and i + 1 < len(codes) and codes[i + 1] == "2" and i + 4 < len(codes):
                        r, g, b = int(codes[i+2]), int(codes[i+3]), int(codes[i+4])
                        # Quantize to 6-bit (0-5 per channel) = 216 colors
                        rq, gq, bq = r // 43, g // 43, b // 43
                        fg = f"#{rq*43:02x}{gq*43:02x}{bq*43:02x}"
                        i += 5
                        continue
                    elif c in ("0", "31", "32", "33", "34", "35", "36", "37"):
                        simple = {"0": "#cdd6f4", "31": "#f38ba8", "32": "#a6e3a1",
                                  "33": "#f9e2af", "34": "#89b4fa", "35": "#f5c2e7",
                                  "36": "#94e2d5", "37": "#bac2de"}
                        fg = simple.get(c, fg)
                    i += 1
            elif part:
                if fg not in tag_cache:
                    tag = f"c{len(tag_cache)}"
                    self.preview.tag_configure(tag, foreground=fg)
                    tag_cache[fg] = tag
                self.preview.insert(tk.END, part, tag_cache[fg])

    def _copy(self):
        if not self.result_text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.result_text)
        self.status_var.set("Copied!")

    def _save_ps1(self):
        """Save as a PowerShell script that displays the ASCII art with colors."""
        if not self.ansi_text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".ps1",
            filetypes=[("PowerShell script", "*.ps1"), ("All", "*.*")]
        )
        if not path:
            return
        ps_lines = ansi_to_ps1_lines(self.ansi_text)
        header = [
            "# ASCII Art — generated by asciify-ps",
            "# Paste this into PowerShell or save and run it.",
            "# Requires: Windows Terminal or PowerShell 7.2+ for true colors.",
            "",
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(header + ps_lines) + "\n")
        self.status_var.set(f"Saved: {os.path.basename(path)}")

    def _save_cmd(self):
        """Save as a .cmd file that displays colored ASCII art."""
        if not self.ansi_text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".cmd",
            filetypes=[("CMD script", "*.cmd *.bat"), ("All", "*.*")]
        )
        if not path:
            return
        # Windows Terminal supports ANSI natively in cmd.exe
        with open(path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("REM ASCII Art — generated by asciify-ps\n")
            f.write("REM Works in Windows Terminal (cmd.exe with ANSI support)\n\n")
            for line in self.ansi_text.split("\n"):
                # Escape % and ! for cmd
                safe = line.replace("%", "%%").replace("!", "^!")
                f.write(f"echo {safe}\n")
        self.status_var.set(f"Saved: {os.path.basename(path)}")

    def _save_txt(self):
        """Save as a .txt file (clean rice ${cN} format, or ANSI, or plain)."""
        if not self.result_text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")]
        )
        if not path:
            return
        fmt = self.output_fmt.get()
        if fmt == "rice":
            data = getattr(self, 'rice_text', self.result_text)  # clean ${cN} format
        elif fmt in ("ansi_truecolor", "ansi_16"):
            data = self.result_text                              # raw ANSI
        else:
            data = self.result_text                              # plain

        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
        self.status_var.set(f"Saved: {os.path.basename(path)}")


def main():
    root = tk.Tk()
    AsciiGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
