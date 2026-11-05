#!/usr/bin/env python3
"""Turn a historical photograph into a watercolor illustration (OpenCV + NumPy).

    python tools/watercolor.py in.jpg out.webp --width 1600 --crop 0,0.5,1,0.76 --seed 7

Pipeline, in the order a painter would work:
  1. tone      - contrast-balanced luminance (CLAHE), so faded prints and photochroms behave alike
  2. palette   - luminance mapped onto a limited palette (indigo shadow, ochre mid, paper light),
                 with a little of the photo's own hue kept where it has any
  3. washes    - edge-preserving flattening (stylization + mean shift) so tones pool into areas
  4. wet edges - pigment collects at the rim of each wash: darken along region boundaries
  5. pigment   - paint is transmittance over paper: density varies with low-frequency blooms and
                 high-frequency granulation, never fully opaque
  6. line      - a light, broken pencil line from the photo's edges (the drawn, editorial feel)
  7. paper     - cold-press grain, and an irregular deckled bleed into blank paper at the borders

Only needed to (re)make the images in static/. The server itself stays standard-library only.
"""
import argparse

import cv2
import numpy as np

def _bgr(*stops):
    """(luminance, (r, g, b)) stops -> (luminance, BGR) for OpenCV."""
    return [(l, (b, g, r)) for l, (r, g, b) in stops]


# One limited palette per colour theme in the app; shadows never reach black, the last stop is the paper.
PALETTES = {
    "newsprint": _bgr((0, (64, 84, 112)), (.30, (98, 122, 150)), (.55, (170, 128, 92)), (.80, (222, 200, 170)), (1, (248, 244, 236))),
    "cover": _bgr((0, (38, 52, 96)), (.30, (66, 100, 168)), (.55, (210, 108, 70)), (.80, (240, 214, 190)), (1, (248, 242, 228))),
    "sage": _bgr((0, (56, 70, 52)), (.30, (112, 136, 100)), (.55, (200, 170, 98)), (.80, (228, 230, 214)), (1, (243, 244, 236))),
    "night": _bgr((0, (40, 52, 74)), (.30, (66, 88, 124)), (.55, (164, 134, 88)), (.80, (206, 198, 178)), (1, (232, 226, 212))),
}
PALETTE = PALETTES["newsprint"]
PAPER = np.array(PALETTE[-1][1], np.float32) / 255


def tone(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(lab[:, :, 0])
    return (l.astype(np.float32) / 255) ** 0.75  # lift mid-tones: watercolour is mostly light


def palette_map(lum):
    stops = np.array([s for s, _ in PALETTE], np.float32)
    cols = np.array([c for _, c in PALETTE], np.float32) / 255
    out = np.empty(lum.shape + (3,), np.float32)
    for ch in range(3):
        out[..., ch] = np.interp(lum, stops, cols[:, ch])
    return out


def noise(shape, scale, rng):
    """Smooth value noise in 0..1 at roughly `scale` pixels per blob."""
    h, w = shape
    small = rng.random((max(2, h // scale), max(2, w // scale))).astype(np.float32)
    big = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    return cv2.normalize(cv2.GaussianBlur(big, (0, 0), scale / 3), None, 0, 1, cv2.NORM_MINMAX)


def watercolor(img, seed=7, palette="newsprint"):
    global PALETTE, PAPER
    PALETTE = PALETTES[palette]
    PAPER = np.array(PALETTE[-1][1], np.float32) / 255
    rng = np.random.default_rng(seed)
    h, w = img.shape[:2]

    lum = tone(img)
    base = palette_map(lum)
    # keep a trace of the photo's own colour (photochroms), muted
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] *= 0.35
    own = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32) / 255
    color = cv2.addWeighted(base, 0.8, own, 0.2, 0)

    # washes: flatten into pooled areas
    u8 = (color * 255).astype(np.uint8)
    flat = cv2.pyrMeanShiftFiltering(cv2.stylization(u8, sigma_s=40, sigma_r=0.3), sp=7, sr=18)
    soft = cv2.bilateralFilter(u8, 9, 40, 9)
    u8 = cv2.addWeighted(flat, 0.55, soft, 0.45, 0)  # pooled areas without poster-hard steps
    wash = u8.astype(np.float32) / 255

    # wet edges: darken along wash boundaries
    g = cv2.cvtColor(u8, cv2.COLOR_BGR2GRAY)
    edges = cv2.morphologyEx(g, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)).astype(np.float32) / 255
    edges = cv2.GaussianBlur(edges, (0, 0), 1.2)
    wash *= (1 - 0.22 * np.clip(edges * 2.0, 0, 1))[..., None]

    # pigment over paper: density with blooms and granulation
    bloom = noise((h, w), 90, rng)
    grain = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 0.8)
    density = np.clip(0.72 + 0.30 * (bloom - 0.5) + 0.12 * (grain - 0.5), 0.45, 0.95)[..., None]
    trans = 1 - density * (1 - wash / PAPER)  # how much paper shows through each wash
    out = PAPER * np.clip(trans, 0, 1)

    # line: light, broken pencil from the photo's edges
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    line = cv2.Canny(cv2.GaussianBlur(gray, (0, 0), 1.8), 50, 130).astype(np.float32) / 255
    line *= (noise((h, w), 10, rng) > 0.42)  # break the line like a moving pencil
    line = cv2.GaussianBlur(line, (0, 0), 0.5)
    out *= (1 - 0.2 * line)[..., None]

    # deckled bleed: irregular fade to blank paper near the borders
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    edge_dist = np.minimum.reduce([xx, w - 1 - xx, yy, h - 1 - yy]) / (0.06 * min(h, w))
    ragged = 0.6 * (noise((h, w), 18, rng) - 0.5) + 0.25 * (noise((h, w), 5, rng) - 0.5)
    mask = np.clip(edge_dist - 0.35 + ragged, 0, 1)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.5)[..., None]
    out = out * mask + PAPER * (1 - mask)

    # paper: cold-press tooth
    tooth = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 1.6)
    out *= (0.965 + 0.07 * (tooth - 0.5))[..., None]
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--crop", default="0,0,1,1", help="x0,y0,x1,y1 as fractions of the photo")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--palette", choices=sorted(PALETTES), default="newsprint")
    a = ap.parse_args()

    img = cv2.imread(a.src, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {a.src}")
    x0, y0, x1, y1 = (float(v) for v in a.crop.split(","))
    h, w = img.shape[:2]
    img = img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
    img = cv2.resize(img, (a.width, round(img.shape[0] * a.width / img.shape[1])), interpolation=cv2.INTER_AREA)
    out = watercolor(img, a.seed, a.palette)
    if not cv2.imwrite(a.dst, out, [cv2.IMWRITE_WEBP_QUALITY, a.quality]):
        raise SystemExit(f"cannot write {a.dst}")
    print(f"{a.dst}: {out.shape[1]}x{out.shape[0]}")


if __name__ == "__main__":
    main()
