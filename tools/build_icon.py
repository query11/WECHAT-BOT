"""Builds the app icon (wechat-assistant-v2.ico) and previews under .runtime/.

Design v3: blue squircle with a soft top light, a translucent back bubble and a
white front bubble carrying an AI sparkle. 16/24 px are drawn separately with
bigger shapes and no back bubble or shadow, so they stay legible in the taskbar
and notification area. TrayDesign.Symbol in tray_theme.cs mirrors this shape.

The file name stays wechat-assistant-v2.ico because window scripts and existing
desktop shortcuts point to it. After rebuilding, refresh Explorer's icon cache
with ``ie4uinit.exe -show`` if a shortcut still shows the old artwork.

    python tools/build_icon.py
"""
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

TOP, BOTTOM = (70, 152, 255), (22, 80, 212)    # background gradient
INK_A, INK_B = (36, 110, 240), (79, 70, 229)   # sparkle gradient: brand blue to indigo
SIZES = (256, 128, 64, 48, 32, 24, 16)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(R, a, b, wx=.35, wy=.65):
    g = Image.new('RGB', (R, R))
    px = g.load()
    for y in range(R):
        for x in range(R):
            px[x, y] = _lerp(a, b, min(1, max(0, (wx * x + wy * y) / R)))
    return g.convert('RGBA')


def _sparkle(cx, cy, r, p=.72, steps=180):
    """Four-point star with concave sides: |x/r|^p + |y/r|^p = 1."""
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + r * math.copysign(abs(c) ** (2 / p), c), cy + r * math.copysign(abs(s) ** (2 / p), s)))
    return pts


def render(size):
    """Returns the icon at ``size`` px; geometry is in 256-unit coordinates."""
    R = max(768, size * 12)
    k = R / 256
    simple = size <= 24          # no back bubble, no shadow, bigger bubble and sparkle
    compact = size <= 48         # keep the back bubble, drop the secondary sparkle
    m = 8 if simple else 16
    mask = Image.new('L', (R, R))
    ImageDraw.Draw(mask).rounded_rectangle([m * k, m * k, (256 - m) * k, (256 - m) * k],
                                           radius=(64 if simple else 58) * k, fill=255)
    img = Image.new('RGBA', (R, R), (0, 0, 0, 0))
    img.paste(_gradient(R, TOP, BOTTOM), (0, 0), mask)

    def layer():
        return Image.new('RGBA', (R, R), (0, 0, 0, 0))

    def rr(draw, box, r, fill):
        draw.rounded_rectangle([v * k for v in box], radius=r * k, fill=fill)

    def poly(draw, pts, fill):
        draw.polygon([(x * k, y * k) for x, y in pts], fill=fill)

    if not simple:
        glow = Image.new('L', (R, R))
        ImageDraw.Draw(glow).ellipse([-50 * k, -80 * k, 190 * k, 118 * k], fill=70)
        glow = Image.composite(glow.filter(ImageFilter.GaussianBlur(30 * k)), Image.new('L', (R, R)), mask)
        light = Image.new('RGBA', (R, R), (255, 255, 255, 0))
        light.putalpha(glow)
        img = Image.alpha_composite(img, light)
        edge = layer()   # thin bright rim fading out below the top edge
        ImageDraw.Draw(edge).rounded_rectangle([m * k, m * k, (256 - m) * k, (256 - m) * k], radius=58 * k,
                                               outline=(255, 255, 255, 46), width=int(2 * k))
        rim = Image.new('L', (R, R))
        ImageDraw.Draw(rim).rectangle([0, 0, R, 120 * k], fill=255)
        edge.putalpha(Image.composite(edge.getchannel('A'), Image.new('L', (R, R)),
                                      rim.filter(ImageFilter.GaussianBlur(40 * k))))
        img = Image.alpha_composite(img, edge)
        back = layer()
        bd = ImageDraw.Draw(back)
        rr(bd, (116, 78, 214, 158), 28, (255, 255, 255, 255))
        poly(bd, [(176, 150), (206, 182), (200, 140)], (255, 255, 255, 255))
        back.putalpha(back.getchannel('A').point(lambda a: a * 118 // 255))
        img = Image.alpha_composite(img, back)

    fb, fr = ((30, 62, 208, 184), 42) if simple else ((42, 90, 184, 186), 34)
    tail = [(52, 170), (44, 218), (100, 178)] if simple else [(62, 174), (54, 212), (104, 180)]
    if not simple:
        shadow = layer()
        sd = ImageDraw.Draw(shadow)
        rr(sd, (fb[0], fb[1] + 9, fb[2], fb[3] + 9), fr, (6, 30, 110, 96))
        poly(sd, [(x, y + 9) for x, y in tail], (6, 30, 110, 96))
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(9 * k)))
    front = layer()
    fd = ImageDraw.Draw(front)
    rr(fd, fb, fr, (255, 255, 255, 255))
    poly(fd, tail, (255, 255, 255, 255))
    img = Image.alpha_composite(img, front)

    cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
    ink = layer()
    idr = ImageDraw.Draw(ink)
    if simple:
        poly(idr, _sparkle(cx, cy + 1, 44, p=.8), (255, 255, 255, 255))
    else:
        poly(idr, _sparkle(cx - 8, cy + 3, 38), (255, 255, 255, 255))
        if not compact:
            poly(idr, _sparkle(cx + 32, cy - 24, 15), (255, 255, 255, 255))
    fill = _gradient(R, INK_A, INK_B, .5, .5)
    fill.putalpha(ink.getchannel('A'))
    img = Image.alpha_composite(img, fill)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main():
    os.chdir(Path(__file__).resolve().parent.parent)
    Path('.runtime').mkdir(exist_ok=True)
    images = {size: render(size) for size in SIZES}
    images[256].save('.runtime/wechat-assistant-icon.png')
    # append_images supplies the hand-tuned small sizes instead of scaled copies.
    images[256].save('wechat-assistant-v2.ico', sizes=[(s, s) for s in SIZES],
                     append_images=[images[s] for s in SIZES if s != 256])
    preview = Image.new('RGBA', (620, 300), (28, 71, 111, 255))
    preview.alpha_composite(images[256], (24, 22))
    x = 310
    for size in (64, 48, 32, 24, 16):
        preview.alpha_composite(images[size], (x, 150 - size // 2))
        x += size + 18
    preview.save('.runtime/icon-preview.png')
    print('ICO sizes:', sorted(Image.open('wechat-assistant-v2.ico').ico.sizes()))


if __name__ == '__main__':
    main()
