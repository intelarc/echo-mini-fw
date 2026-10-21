#!/usr/bin/env python3
"""Generate the "Retro" theme PNGs from an unpacked V1.9.0 work folder.

    python themes/retro/make_retro.py v190 themes/retro/build

White slot (images 093-455) -> iPod-video look: clean silver/white, blue accents and
blue gradient list highlight. Black slot (456-818) -> 2007 Walkman look: deeper black,
orange accents and orange list highlight. Shared boot (010-044) and shutdown (045)
frames are redrawn. Pure red (255,0,0) and pure black are the firmware's transparency
keys and are never changed. Needs Pillow and numpy.
"""
import json, math, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
WHITE = range(93, 456)
BLACK = range(456, 819)
SCREEN = (480, 222)

# White-slot indices; the Black slot is +363 (the firmware adds 363 when the theme is black).
SEL_BARS = [168, 169, 170, 171, 172, 173, 174, 175, 176]       # list highlight bars
MAIN_MENU_PAGES = [100, 101, 102, 103, 104, 105]
# Red stays red: the A-B badges may be keyed on red (their letters are fringed with it),
# and the low-battery icons and favourite hearts are red on purpose.
KEEP_RED = {126, 127, 128, 129, 130, 363, 364, 365, 366}

IPOD = {'hue': 212, 'gamma': 0.85, 'tint': (-0.012, 0.0, 0.028), 'tile_gain': 1.9,
        'bar': [(176, 214, 252), (112, 172, 242), (78, 146, 232)], 'bar_edge': (44, 104, 196)}
WALKMAN = {'hue': 29, 'gamma': 1.22, 'tint': (-0.004, 0.0, 0.010), 'tile_gain': 1.0,
           'bar': [(214, 120, 30), (178, 88, 14), (140, 62, 6)], 'bar_edge': (96, 40, 0)}


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def hue_unit(h):
    """RGB of a fully saturated, full-value colour at hue h (degrees)."""
    h6 = (h % 360) / 60.0
    i, f = int(h6), h6 - int(h6)
    return np.array([(1, f, 0), (1 - f, 1, 0), (0, 1, f), (0, 1 - f, 1), (f, 0, 1), (1, 0, 1 - f)][i])


def recolor(im, style, map_red=True):
    """Neutrals get the style's tone curve; reds rotate to the accent hue unless map_red is off.

    Pure red with hard edges is the firmware's transparency key (overlay strips, the USB-DAC
    frames' hole for the page dots) and is kept exactly; anti-aliased pure red is a visible
    colour and gets the accent like any other red. With map_red off, all reds are left as is.
    """
    a = np.asarray(im.convert('RGB')).astype(np.float32) / 255
    pure = np.all(a == [1, 0, 0], -1)
    keys = np.all(a == 0, -1)
    if pure.any():
        near = np.zeros_like(pure)
        near[1:] |= pure[:-1]; near[:-1] |= pure[1:]; near[:, 1:] |= pure[:, :-1]; near[:, :-1] |= pure[:, 1:]
        edge = near & ~pure
        reddish = (a[..., 0] > a[..., 1] + 0.15) & (a[..., 0] > a[..., 2] + 0.15)
        if (edge & reddish).sum() < 0.5 * max(edge.sum(), 1):
            keys |= pure
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    hue = np.degrees(np.arctan2(math.sqrt(3) * (g - b), 2 * r - g - b)) % 360
    luma = 0.299 * r + 0.587 * g + 0.114 * b

    # neutrals: tone curve plus a gentle tint in the mid-tones
    n = (luma ** style['gamma'])[..., None] + (4 * luma * (1 - luma))[..., None] * np.array(style['tint'])
    # reds: rotate to the accent hue, keeping saturation and value
    accent = mx[..., None] * ((1 - s)[..., None] + s[..., None] * hue_unit(style['hue']))
    dh = np.abs((hue + 180) % 360 - 180)
    w_red = (1 - smoothstep(16, 32, dh))[..., None] * map_red
    w_sat = smoothstep(0.10, 0.32, s)[..., None]
    out = (1 - w_sat) * n + w_sat * (w_red * accent + (1 - w_red) * a)
    out = np.where(keys[..., None], a, np.clip(out, 0, 1))
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8))


def gradient_bar(size, style):
    """Glossy list highlight: three-stop vertical gradient, 1px edge, lighter top line."""
    w, h = size
    top, mid, bot = (np.array(c, np.float32) for c in style['bar'])
    t = np.linspace(0, 1, h)[:, None]
    col = np.where(t < 0.5, top + (mid - top) * (t / 0.5), mid + (bot - mid) * ((t - 0.5) / 0.5))
    a = np.repeat(col[:, None, :], w, axis=1).reshape(h, w, 3)
    a[0, :] = np.minimum(top + 30, 255)
    edge = np.array(style['bar_edge'], np.float32)
    a[-1, :] = edge
    a[:, 0] = a[:, -1] = (a[:, 0] + edge) / 2
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def tint_selected_tiles(pages, style):
    """Main-menu pages differ in which bottom tile (rows 165+) and page dot are lit; tint those.

    The tape reels also differ between pages, so above the tiles only bright pixels (the dot) count.
    """
    arr = np.stack([np.asarray(p).astype(np.float32) for p in pages])
    med = np.median(arr, 0)
    unit = hue_unit(style['hue'])
    rows = np.arange(arr.shape[1])[:, None]
    out = []
    for p in arr:
        lum = p.mean(-1)
        band = (rows >= 165) | ((rows >= 30) & (rows <= 58) & (lum > 140))
        lit = (smoothstep(6, 24, np.abs(lum - med.mean(-1))) * band)[..., None]
        gain = np.where(rows >= 165, style['tile_gain'], 1.0)[..., None]
        accent = np.clip(lum[..., None] * gain * (0.25 + 0.95 * unit), 0, 255)
        keep_icon = (smoothstep(170, 235, lum) * (rows >= 165))[..., None]   # white glyph on a dark tile
        accent = accent * (1 - keep_icon) + p * keep_icon
        out.append(Image.fromarray(np.clip(p * (1 - lit) + accent * lit, 0, 255).astype(np.uint8)))
    return out


# --- boot / shutdown art --------------------------------------------------------------

FONT_DIR = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
SS = 4  # supersampling


def font(name, px):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), px * SS)


def logo_layer(ring_cx, ring_cy, ring_progress, play_alpha, word_reveal, tag_alpha, reflect=True,
               word='echo', tagline='DIGITAL AUDIO PLAYER'):
    """Draw the boot logo state on a black 480x222 canvas (supersampled)."""
    W, H = SCREEN[0] * SS, SCREEN[1] * SS
    img = Image.new('RGB', (W, H), 0)
    d = ImageDraw.Draw(img)
    R, stroke = 32 * SS, 6 * SS
    cx, cy = ring_cx * SS, ring_cy * SS
    floor = cy + R                     # ring bottom and wordmark baseline share this line
    if ring_progress > 0:
        box = [cx - R, cy - R, cx + R, cy + R]
        d.arc(box, -90, -90 + 360 * ring_progress, fill=(236, 238, 242), width=stroke)
    if play_alpha > 0:
        v = int(236 * play_alpha)
        s = 16 * SS
        d.polygon([(cx - s * 0.55, cy - s), (cx - s * 0.55, cy + s), (cx + s * 1.0, cy)], fill=(v, v, v))
    word_x0 = (ring_cx + 44) * SS
    if word_reveal > 0:
        f = font('seguisb.ttf', 62)
        wl = Image.new('L', (W, H), 0)
        ImageDraw.Draw(wl).text((word_x0, floor), word, font=f, fill=236, anchor='ls')
        right = word_x0 + (ImageDraw.Draw(wl).textlength(word, font=f) + 4 * SS) * word_reveal
        mask = Image.new('L', (W, H), 0)
        ImageDraw.Draw(mask).rectangle([0, 0, right, H], fill=255)
        img.paste((236, 238, 242), mask=Image.composite(wl, Image.new('L', (W, H), 0), mask))
    if reflect and (ring_progress > 0 or word_reveal > 0):
        # Y2K glossy reflection under the logo, fading out
        src = img.crop((0, floor - R, W, floor)).transpose(Image.FLIP_TOP_BOTTOM)
        fade = np.linspace(0.30, 0, src.height)[:, None, None]
        refl = Image.fromarray((np.asarray(src).astype(np.float32) * fade).astype(np.uint8))
        img.paste(refl, (0, floor + 2 * SS))
    if tag_alpha > 0:
        f = font('bahnschrift.ttf', 12)
        v = int(170 * tag_alpha)
        d.text((W // 2, (ring_cy + 98) * SS), ' '.join(tagline), font=f, fill=(v, v, v), anchor='mm')
    return img.resize(SCREEN, Image.LANCZOS)


def ease(t):
    t = min(max(t, 0), 1)
    return t * t * (3 - 2 * t)


def boot_frames(sizes):
    """Full-screen states for frames 10..44 (drawn at the top-left, cropped per frame)."""
    frames = {}
    cx_mid, cy = 240, 92
    final_cx = 162
    frames[10] = Image.new('RGB', SCREEN, 0)
    ImageDraw.Draw(frames[10]).ellipse([cx_mid - 2, cy - 2, cx_mid + 2, cy + 2], fill=(236, 238, 242))
    for k in range(11, 26):            # ring draws in, then the play mark appears
        t = (k - 11) / 14
        frames[k] = logo_layer(cx_mid, cy, ease(t / 0.75), ease((t - 0.6) / 0.4), 0, 0, reflect=False)
    for k in range(26, 35):            # slide left to make room for the wordmark
        t = ease((k - 25) / 9)
        frames[k] = logo_layer(cx_mid + (final_cx - cx_mid) * t, cy, 1, 1, 0, 0, reflect=False)
    for k in range(35, 44):            # wordmark wipes in, reflection and tagline fade up
        t = (k - 35) / 8
        frames[k] = logo_layer(final_cx, cy, 1, 1, ease(t / 0.7), ease((t - 0.5) / 0.5))
    frames[44] = logo_layer(final_cx, cy, 1, 1, 1, 1)
    return frames


def crop_and_check(frames, sizes):
    """Crop each state to its frame size and simulate top-left drawing to catch leftovers."""
    screen = np.zeros((SCREEN[1], SCREEN[0], 3), np.int16)
    out = {}
    for k in sorted(frames):
        w, h = sizes[k]
        full = np.asarray(frames[k]).astype(np.int16)
        outside = full.copy()
        outside[:h, :w] = 0
        if outside.max() > 8:
            sys.exit(f'boot frame {k}: content outside its {w}x{h} crop')
        screen[:h, :w] = full[:h, :w]
        if np.abs(screen - full).max() > 8:
            sys.exit(f'boot frame {k}: previous frame would leave pixels behind')
        out[k] = frames[k].crop((0, 0, w, h))
    return out


def shutdown_frame():
    img = logo_layer(162, 92, 1, 1, 1, 0)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(os.path.join(FONT_DIR, 'bahnschrift.ttf'), 12)
    d.text((240, 190), ' '.join('GOODBYE'), font=f, fill=(170, 170, 170), anchor='mm')
    return img


# --- main --------------------------------------------------------------------------------

def main():
    work, out = sys.argv[1], sys.argv[2]
    overrides = [os.path.join(HERE, '..', '..', 'mods', 'images')]
    os.makedirs(out, exist_ok=True)
    manifest = json.load(open(os.path.join(work, 'manifest.json'), encoding='utf-8'))
    names = {m['i']: m['png'] for m in manifest['images']}
    sizes = {m['i']: (m['w'], m['h']) for m in manifest['images']}

    def load(i):
        for d in overrides:
            p = os.path.join(d, names[i])
            if os.path.exists(p):
                return Image.open(p).convert('RGB')
        return Image.open(os.path.join(work, 'images', names[i])).convert('RGB')

    result = {}
    for i in WHITE:
        result[i] = recolor(load(i), IPOD, i not in KEEP_RED)
    for i in BLACK:
        result[i] = recolor(load(i), WALKMAN, i - 363 not in KEEP_RED)
    for style, base in ((IPOD, 0), (WALKMAN, 363)):
        for i in SEL_BARS:
            result[i + base] = gradient_bar(sizes[i + base], style)
        pages = [i + base for i in MAIN_MENU_PAGES]
        for i, im in zip(pages, tint_selected_tiles([result[i] for i in pages], style)):
            result[i] = im
    result.update(crop_and_check(boot_frames(sizes), sizes))
    result[45] = shutdown_frame()

    for i, im in result.items():
        if im.size != sizes[i]:
            sys.exit(f'image {i} is {im.size}, expected {sizes[i]}')
        im.save(os.path.join(out, names[i]))
    print(f'wrote {len(result)} images to {out}')


if __name__ == '__main__':
    main()
