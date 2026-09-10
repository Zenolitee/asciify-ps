<p align="center">
  <img src="docs/screenshot.png" alt="asciify-ps — image to ASCII art GUI" width="880">
</p>

<h1 align="center">asciify-ps</h1>

<p align="center">
  <em>A small desktop app that turns images into colored ASCII art — with a live preview.</em>
</p>

---

## What it is

**asciify-ps** is a Windows desktop GUI (Python + Tkinter) wrapped around the
[`asciify-them`](https://github.com/ndrscalia/asciify-them) rendering library.

Load an image, drag the width slider, and the ASCII rendering updates as you go.
Tweak the charset, colors, edge detection or inversion and watch the result change
in real time — then copy it to the clipboard or save it as a text file.

## Features

- **Live preview** — the output re-renders automatically (350 ms debounce) as you change any setting.
- **Width control** — 20–300 columns, via slider or by typing a value.
- **Color modes** — full color or black & white.
- **Edge detection** — highlights contours for line-art style output.
- **Invert** — flips the source image before conversion.
- **Charset presets** — `default`, `classic`, `extended`, `braille`, `unicode_blocks`.
- **Three output formats**:
  | Format | Description |
  | --- | --- |
  | **Rice `${cN}`** | Color-quantized palette format. Emits a `(palette c1=R,G,B …)` header plus inline `${cN}` markers, using 2–24 colors picked from the image itself by k-means. |
  | **True Color** | Raw 24-bit ANSI escape sequences. |
  | **Plain** | ANSI stripped — plain monochrome text. |
- **Export** — copy to clipboard or save the current output as `.txt`.
- Dark theme UI.

## Requirements

- **Windows** (the UI and the terminal-size detection are Windows-oriented).
- **Python 3.9+** with `tkinter` (bundled with the official Windows installer).
- Runtime packages: `numpy`, `opencv-python`, `Pillow` — see `requirements.txt`.

## Install and run

```bash
git clone https://github.com/Zenolitee/asciify-ps.git
cd asciify-ps
pip install -r requirements.txt
python asciify_ps.py
```

On Windows you can also just double-click **`asciify-ps.bat`**, which installs
nothing but launches the app from its own folder.

## Usage

1. Click **📂 Open** and pick an image (`.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp`, `.gif`).
2. Adjust **WIDTH**, **Color**, **Charset**, **Output** format and the **Colors**
   count for the Rice palette. The preview refreshes on its own.
3. Use **📋 Copy** to put the result on the clipboard, or **💾 Save .txt** to write it out.

Toggle **Edges** for contour highlighting and **Invert** to flip the image.
The status bar shows the rendered line count.

## Project layout

```
asciify_ps.py        # the Tkinter GUI application
asciify-ps.bat       # double-click launcher for Windows
bring_front.ps1      # optional helper: brings the app window to the foreground
requirements.txt     # runtime dependencies
asciify-them/        # vendored rendering library (MIT) — see its LICENSE
  ├── LICENSE
  ├── requirements.txt
  └── src/asciify/   # core, process, renderer, utils, cli
docs/screenshot.png  # screenshot used above
```

`asciify_ps.py` adds `asciify-them/src` to `sys.path`, so the vendored library
works with no installation step.

## Credits

The ASCII rendering engine is **[asciify-them](https://github.com/ndrscalia/asciify-them)**
by **Andrea Scalia**, redistributed here under its MIT license. It is itself
partially based on **[ascii-view](https://github.com/gouwsxander/ascii-view)** by
Xander Gouws. The vendored copy is included for convenience; its original
`LICENSE` is kept at `asciify-them/LICENSE`.

## License

The application code in this repository is released under the **MIT License** —
see [LICENSE](LICENSE). The vendored `asciify-them` library remains under its own
MIT license, reproduced at `asciify-them/LICENSE`.
