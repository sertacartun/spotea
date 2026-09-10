"""Generates the apple-touch-startup-image PNG(s) iOS shows during a
standalone PWA's WKWebView boot — the ~300-800ms window before any of the
app's own HTML/CSS can paint anything, which iOS fills with a plain white
screen unless a static image matches the launching device's *exact* physical
pixel size (see _base.html's <link rel="apple-touch-startup-image"> for why
there is no manifest-driven equivalent of Android's auto-generated splash
here).

Flat --bg-page, nothing else. An earlier version drew the coffee-cup icon and
wordmark here too, matched pixel-for-pixel against #app-splash's real SVG —
but iOS's own zoom-in launch transition dims/scales this image while it
animates up from the home-screen icon, and a flat color dimmed is still that
color, where a detailed icon dimmed reads as a wrong-colored, misshapen one
for the length of that animation. Flat avoids the whole class of mismatch
rather than chasing it, at the cost of a plain screen for the boot window
instead of a branded one — #app-splash still carries the real icon from the
moment the page can paint it.

Add an entry to DEVICES and re-run to cover another device; each is written
to app/static/img/apple-splash-<physical width>x<physical height>.png and has
to be wired up with its own <link> (media-queried on device-width,
device-height and -webkit-device-pixel-ratio) in _base.html.
"""

from pathlib import Path

from PIL import Image

# device-width x device-height (pt, portrait) x device pixel ratio.
DEVICES = [
    (430, 932, 3),  # iPhone 15 Pro Max / 16 Plus
]

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "app" / "static" / "img"

BG = (2, 4, 7)  # --bg-page


def generate(device_width, device_height, scale):
    w, h = device_width * scale, device_height * scale
    img = Image.new("RGB", (w, h), BG)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"apple-splash-{w}x{h}.png"
    img.save(out_path, "PNG")
    return out_path


if __name__ == "__main__":
    for device_width, device_height, scale in DEVICES:
        path = generate(device_width, device_height, scale)
        print(f"wrote {path}")
