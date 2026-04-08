"""
Generate icon.ico for RS2IR Converter.
Renders guitar emoji on a dark purple background using Windows' Segoe UI Emoji
font, then bakes a multi-size ICO.

Run once before building:
    python generate_icon.py
"""

from PIL import Image, ImageDraw, ImageFont

# Palette (matches app GUI)
BG_COLOR     = (30, 30, 46, 255)    # #1e1e2e
EMOJI_COLOR  = (167, 139, 250)      # #a78bfa

EMOJI_FONT   = r"C:\Windows\Fonts\seguiemj.ttf"   # Segoe UI Emoji (Windows 10/11)

RENDER_SIZE  = 256
ICO_SIZES    = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
OUTPUT       = "icon.ico"
SUPER        = 4    # render at 4x then downscale for antialiasing


def _load_font(size):
    try:
        return ImageFont.truetype(EMOJI_FONT, size)
    except OSError:
        return ImageFont.load_default()


def make_master():
    """Render the icon at RENDER_SIZE px using supersampling."""
    ss = RENDER_SIZE * SUPER

    img  = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    radius = ss // 8
    draw.rounded_rectangle([(0, 0), (ss - 1, ss - 1)], radius=radius, fill=BG_COLOR)

    cx, cy = ss // 2, ss // 2

    # Guitar emoji - centered, fills most of the frame
    font_g = _load_font(int(ss * 0.82))
    draw.text((cx, cy), "\U0001f3b8",  # guitar
              font=font_g, anchor="mm",
              fill=EMOJI_COLOR, embedded_color=True)

    # Downscale with Lanczos for smooth edges
    return img.resize((RENDER_SIZE, RENDER_SIZE), Image.LANCZOS)


def main():
    print("Rendering master frame ...")
    master = make_master()
    master.save(OUTPUT, format="ICO", sizes=ICO_SIZES)
    print("Saved: " + OUTPUT + "  (" + str(len(ICO_SIZES)) + " sizes: " + str([s[0] for s in ICO_SIZES]) + ")")


if __name__ == "__main__":
    main()
