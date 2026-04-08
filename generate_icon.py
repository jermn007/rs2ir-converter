"""
Generate icon.ico for RS2IR Converter.
Renders guitar (guitar emoji) + VR goggles (goggles emoji) on a dark purple
background using Windows' Segoe UI Emoji font, then bakes a multi-size ICO.

Run once before building:
    python generate_icon.py
"""

from PIL import Image, ImageDraw, ImageFont

# ── Palette (matches app GUI) ───────────────────────────────────────────────
BG_COLOR     = (30, 30, 46, 255)    # #1e1e2e
EMOJI_COLOR  = (167, 139, 250)      # #a78bfa

EMOJI_FONT   = r"C:\Windows\Fonts\seguiemj.ttf"   # Segoe UI Emoji (Windows 10/11)

# Pillow ICO encoder resamples from a single source image; render large and
# let it produce all standard sizes via thumbnail downscale.
RENDER_SIZE  = 256
ICO_SIZES    = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
OUTPUT       = "icon.ico"
SUPER        = 4    # render at 4× then downscale for antialiasing


def make_master() -> Image.Image:
    """Render the icon at RENDER_SIZE px using supersampling."""
    ss = RENDER_SIZE * SUPER

    img  = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    radius = ss // 8
    draw.rounded_rectangle([(0, 0), (ss - 1, ss - 1)], radius=radius, fill=BG_COLOR)

    cx, cy = ss // 2, ss // 2

    # Guitar — left of centre, large
    try:
        font_g = ImageFont.truetype(EMOJI_FONT, int(ss * 0.55))
    except OSError:
        font_g = ImageFont.load_default()

    draw.text((int(cx * 0.70), cy), "\U0001f3b8",   # 🎸
              font=font_g, anchor="mm",
              fill=EMOJI_COLOR, embedded_color=True)

    # VR goggles — top-right badge, smaller
    try:
        font_v = ImageFont.truetype(EMOJI_FONT, int(ss * 0.30))
    except OSError:
        font_v = font_g

    draw.text((int(ss * 0.80), int(ss * 0.22)), "\U0001f97d",  # 🥽
              font=font_v, anchor="mm",
              fill=EMOJI_COLOR, embedded_color=True)

    # Downscale with Lanczos for smooth edges
    return img.resize((RENDER_SIZE, RENDER_SIZE), Image.LANCZOS)


def main():
    print("Rendering master frame ...")
    master = make_master()
    master.save(OUTPUT, format="ICO", sizes=ICO_SIZES)
    print(f"Saved {OUTPUT}  ({len(ICO_SIZES)} sizes: {[s[0] for s in ICO_SIZES]})")


if __name__ == "__main__":
    main()
