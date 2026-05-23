"""Generate app icons for Windows (.ico) and macOS (.png) at build time."""
from PIL import Image, ImageDraw
import sys


def make_frame(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = max(4, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(30, 58, 95))

    # Waveform bars (5 bars, tallest in center)
    bar_color = (255, 255, 255)
    cx, cy = size // 2, size // 2
    bar_w = max(2, size // 16)
    gap = max(2, size // 14)
    heights = [0.28, 0.52, 0.72, 0.52, 0.28]
    n = len(heights)
    total_w = n * bar_w + (n - 1) * gap
    x = cx - total_w // 2
    for h in heights:
        bh = int(size * h)
        by = cy - bh // 2
        draw.rounded_rectangle(
            [x, by, x + bar_w, by + bh], radius=max(1, bar_w // 2), fill=bar_color
        )
        x += bar_w + gap
    return img


# Windows .ico — multiple sizes embedded
sizes = [16, 32, 48, 64, 128, 256]
imgs = [make_frame(s) for s in sizes]
imgs[0].save(
    "icon.ico",
    format="ICO",
    sizes=[(s, s) for s in sizes],
    append_images=imgs[1:],
)
print("Generated icon.ico")

# macOS / Linux — high-res PNG (PyInstaller uses this)
make_frame(1024).save("icon.png", format="PNG")
print("Generated icon.png")
