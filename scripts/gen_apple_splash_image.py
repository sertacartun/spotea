"""Generates the flat apple-touch-startup-image PNGs iOS shows while a standalone PWA boots.

iOS only uses an image matching the device's exact physical pixel size; each new DEVICES entry
also needs its own media-queried <link> in _base.html. Flat on purpose: iOS dims the image
during its launch animation, which makes a detailed icon look wrong.
"""

from pathlib import Path

from PIL import Image

# device-width x device-height (pt, portrait) x device pixel ratio.
DEVICES = [
    (430, 932, 3),  # iPhone 15 Pro Max / 16 Plus
]

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "app" / "static" / "img"

BG = (3, 3, 3)


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
