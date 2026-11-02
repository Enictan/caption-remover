"""Draws the Caption Remover app icon and writes .png / .icns (Mac) / .ico (Windows)."""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024
SS = 4  # supersample for smooth edges
W = S * SS


def rr(d, box, r, **kw):
    d.rounded_rectangle([v * SS for v in box], radius=r * SS, **kw)


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# macOS-style squircle tile (824px body inside 1024 canvas, per Apple grid)
pad = 100
tile = Image.new("RGBA", (W, W), (0, 0, 0, 0))
td = ImageDraw.Draw(tile)
# vertical gradient body
grad = Image.new("RGBA", (W, W))
gd = ImageDraw.Draw(grad)
for y in range(W):
    t = y / W
    c = (int(34 - 14 * t), int(32 - 13 * t), int(38 - 14 * t), 255)
    gd.line([(0, y), (W, y)], fill=c)
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([pad * SS, pad * SS, (S - pad) * SS, (S - pad) * SS], radius=185 * SS, fill=255)
# soft shadow
sh = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(sh).rounded_rectangle([pad * SS, (pad + 14) * SS, (S - pad) * SS, (S - pad + 14) * SS], radius=185 * SS, fill=(0, 0, 0, 120))
sh = sh.filter(ImageFilter.GaussianBlur(18 * SS))
img.alpha_composite(sh)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)

GOLD = (212, 162, 76, 255)
GOLD2 = (240, 196, 108, 255)

# film frame
rr(d, (230, 260, 794, 640), 46, outline=GOLD, width=22 * SS)
# horizon inside the frame (the "clean" picture)
d.line([(270 * SS, 520 * SS), (420 * SS, 450 * SS), (520 * SS, 505 * SS), (620 * SS, 430 * SS), (754 * SS, 500 * SS)],
       fill=(212, 162, 76, 110), width=14 * SS, joint="curve")
d.ellipse([640 * SS, 315 * SS, 710 * SS, 385 * SS], fill=(212, 162, 76, 120))

# caption bars below the frame: left part solid, right part dissolving
rr(d, (270, 700, 560, 748), 24, fill=GOLD)
rr(d, (300, 780, 470, 820), 20, fill=(212, 162, 76, 170))
# dissolving particles
import random
random.seed(7)
for i in range(46):
    x = 575 + random.random() * 200
    y = 705 + random.random() * 115
    fade = 1 - (x - 575) / 220
    r = 4 + 12 * fade * random.random()
    a = int(255 * max(0.15, fade))
    d.ellipse([(x - r) * SS, (y - r) * SS, (x + r) * SS, (y + r) * SS], fill=(240, 196, 108, a))

# sparkle
cx, cy, R = 744, 690, 48
pts = [(cx, cy - R), (cx + 12, cy - 12), (cx + R, cy), (cx + 12, cy + 12), (cx, cy + R), (cx - 12, cy + 12), (cx - R, cy), (cx - 12, cy - 12)]
d.polygon([(x * SS, y * SS) for x, y in pts], fill=GOLD2)

icon = img.resize((S, S), Image.LANCZOS)
icon.save(os.path.join(HERE, "icon.png"))
icon.save(os.path.join(HERE, "AppIcon.icns"))
icon.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written")
