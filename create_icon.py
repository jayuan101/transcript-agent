#!/usr/bin/env python3
"""Generate icon.ico (Windows) and icon.icns (Mac) for TranscriptAgent."""
import os
import sys


def make_base_image(size):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark-blue rounded-square background
    r = size // 5
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(26, 35, 126, 255))

    # Audio waveform bars (7 bars, symmetric)
    n_bars = 7
    heights = [0.30, 0.55, 0.75, 0.90, 0.75, 0.55, 0.30]
    bar_w = size // (n_bars * 2 + 1)
    total_w = n_bars * bar_w + (n_bars - 1) * bar_w
    start_x = (size - total_w) // 2
    cy = size // 2

    for i, h in enumerate(heights):
        bar_h = int(size * h * 0.60)
        x = start_x + i * bar_w * 2
        y1 = cy - bar_h // 2
        y2 = cy + bar_h // 2
        draw.rounded_rectangle(
            [x, y1, x + bar_w, y2],
            radius=bar_w // 2,
            fill=(100, 181, 246, 255),
        )

    return img


def build_windows():
    from PIL import Image

    base = make_base_image(256)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    images = [base.resize(s, Image.LANCZOS) for s in sizes]
    images[0].save("icon.ico", format="ICO", sizes=sizes, append_images=images[1:])
    print("Created icon.ico")


def build_mac():
    import subprocess
    from PIL import Image

    iconset = "icon.iconset"
    os.makedirs(iconset, exist_ok=True)
    specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    base = make_base_image(1024)
    for px, name in specs:
        base.resize((px, px), Image.LANCZOS).save(f"{iconset}/{name}")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", "icon.icns"], check=True)
    print("Created icon.icns")


if __name__ == "__main__":
    if "--windows" in sys.argv:
        build_windows()
    elif "--mac" in sys.argv:
        build_mac()
    elif sys.platform == "win32":
        build_windows()
    elif sys.platform == "darwin":
        build_mac()
    else:
        print("Pass --windows or --mac")
        sys.exit(1)
