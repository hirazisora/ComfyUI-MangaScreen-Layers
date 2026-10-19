"""Tone layers (fork addition): the Manga Finish pipeline split into editable layers.

Manga Finish Layers: the Manga Finish pipeline (copied from __init__.py and extended, so the layers
stay stable when the original node changes), but instead of one flattened black-and-white page it
returns three transparent RGBA layers:

  light_tone  - the light (thin) tones as a smooth gradation mask (or, optionally, screentone dots)
  dark_tone   - the dark (dense) tones as a smooth gradation mask (or, optionally, screentone dots)
  solid       - beta (solid black) and pen lines, with the white cut-out lines removed

Extensions over Manga Finish: line_cleanup (stroke detection that ignores grain and highlight rims),
tone_flatten, line_antialias, and level_mode auto (white / black point from a multi-Otsu split).

Manga Solid Refine (Tone Fill): takes the solid layer redrawn by an edit model (Qwen Image) as clean
line art + beta, and hands whatever was solid before and is gone now back to the tone layers.

Manga Layer Flatten: stacks RGBA layers onto paper as a plain RGB image.

Every layer is ink colour on a transparent background (RGB = ink colour, alpha = ink density).
"""
import cv2
import numpy as np
import torch

ANGLE = 45.0
SOLID_COVER = 0.98     # dot coverage at or above this is merged into the solid layer


# ---------------------------------------------------------------------------------------------
# Manga Finish pipeline (from ComfyUI-MangaScreen, MIT)
# ---------------------------------------------------------------------------------------------

def print_screen(cover, pitch):
    """Page-aligned AM dots for a coverage map, rendered 3x supersampled so dots are round."""
    h, w = cover.shape
    s = 3
    y, x = np.mgrid[0:h * s, 0:w * s].astype(np.float32)
    a = np.deg2rad(ANGLE)
    u = (x * np.cos(a) + y * np.sin(a)) / (pitch * s)
    v = (-x * np.sin(a) + y * np.cos(a)) / (pitch * s)
    spot = 1 - (np.cos(2 * np.pi * u) + np.cos(2 * np.pi * v) + 2) / 4
    c = cv2.resize(cover, (w * s, h * s), interpolation=cv2.INTER_LINEAR)
    return cv2.resize((spot < c).astype(np.float32), (w, h), interpolation=cv2.INTER_AREA) > 0.5


def ink_layer(L, k, cleanup=0.0):
    """Lines and solid blacks as a crisp mask. k is the image scale relative to 1024 px width.
    cleanup > 0 keeps only real strokes: thin features clearly darker than what surrounds them.
    The plain local-mean test also fires on a grey (grainy) area right next to a white highlight,
    which turns hair with highlight strokes into ragged black fuzz."""
    solid = cv2.GaussianBlur(L, (0, 0), 1.5 * k) < 0.22
    Lb = cv2.GaussianBlur(L, (0, 0), 0.7 * k)
    line = (Lb < cv2.blur(Lb, (int(15 * k) | 1,) * 2) - 0.07) & (Lb < 0.65)
    if cleanup:
        # black-hat on a descreened copy: how much darker a pixel is than the closing over a
        # window wider than a stroke. A broad grey area scores ~0, grain is blurred away.
        Ls = cv2.GaussianBlur(L, (0, 0), 1.2 * k)
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(9 * k) | 1,) * 2)
        line &= cv2.morphologyEx(Ls, cv2.MORPH_BLACKHAT, ker) > 0.3 * cleanup
    _, lab, st, _ = cv2.connectedComponentsWithStats(line.astype(np.uint8), connectivity=8)
    w, h, area = st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT], st[:, cv2.CC_STAT_AREA]
    # a stroke is long and thin; a dot, or a clump of generated dots, is compact
    keep = (np.maximum(w, h) >= 10 * k) & (area >= 12 * k * k) & (area < 0.6 * w * h + 40 * k * k)
    keep |= area >= 400 * k * k
    keep[0] = False
    ink = solid | keep[lab]
    _, lab, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    big = st[:, cv2.CC_STAT_AREA] >= 6 * k * k
    big[0] = False
    return big[lab]


def value_map(L, ink, k):
    """Descreened value with the ink masked out (normalized convolution), then edge-preserving flattening."""
    m = (~ink).astype(np.float32)
    s = 2.5 * k
    V = cv2.GaussianBlur(L * m, (0, 0), s) / np.maximum(cv2.GaussianBlur(m, (0, 0), s), 1e-3)
    for _ in range(2):
        V = cv2.bilateralFilter(V, int(9 * k) | 1, 0.10, 9 * k)
    return V


def tone_plan(V, k, white, split, black, min_region=600):
    """0 white, 1 light tone, 2 dark tone, 3 black. Small regions melt into their surroundings."""
    level = cv2.medianBlur(np.digitize(-V, [-white, -split, -black]).astype(np.uint8), int(11 * k) | 1)
    for lv in (1, 2, 3, 0):
        _, lab, st, _ = cv2.connectedComponentsWithStats((level == lv).astype(np.uint8), connectivity=4)
        small = st[:, cv2.CC_STAT_AREA] < min_region * k * k
        small[0] = False
        hole = small[lab]
        if hole.any():
            fill = cv2.medianBlur(np.where(hole, 255, level).astype(np.uint8), int(21 * k) | 1)
            level = np.where(hole & (fill != 255), fill, level).astype(np.uint8)
    return level


def region_spread(level, V):
    """Standard deviation of V inside each flat region of the plan: a region hiding a gradient spreads wide."""
    out = np.zeros_like(V)
    for lv in range(4):
        n, lab = cv2.connectedComponents((level == lv).astype(np.uint8), connectivity=4)
        cnt = np.maximum(np.bincount(lab.ravel(), minlength=n), 1)
        mean = np.bincount(lab.ravel(), V.ravel(), minlength=n) / cnt
        std = np.sqrt(np.maximum(np.bincount(lab.ravel(), (V * V).ravel(), minlength=n) / cnt - mean * mean, 0))
        out = np.where(level == lv, std[lab], out)
    return out.astype(np.float32)


def white_cut(candidates, k):
    """Long strokes only, thinned: the folds and seams kept in white inside beta."""
    _, lab, st, _ = cv2.connectedComponentsWithStats(candidates.astype(np.uint8), connectivity=8)
    keep = np.maximum(st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT]) >= 40 * k
    keep[0] = False
    return cv2.erode(keep[lab].astype(np.uint8), np.ones((2 if k < 1.6 else 3,) * 2, np.uint8)) > 0


def highlight_map(L, k, white=0.8):
    """Thin white strokes (hair glints, sheen) as a soft 0..1 mask: how much brighter a pixel is than
    the opening over a window wider than a stroke, on a descreened copy. The tone layers are punched
    out there, so highlights stay crisp instead of melting into the smoothed tone.
    Only paper-bright pixels count (near `white`, the paper threshold in use): a grey strand between
    black hair, or a shadow squeezed between two lines, also stands out from its darker surroundings
    in the top-hat, but it is tone and must not be punched out."""
    Ls = cv2.GaussianBlur(L, (0, 0), 1.0 * k)
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(11 * k) | 1,) * 2)
    g = np.clip((cv2.morphologyEx(Ls, cv2.MORPH_TOPHAT, ker) - 0.15) / 0.15, 0, 1)
    g *= np.clip((Ls - (white - 0.12)) / 0.08, 0, 1)
    # a glint is a stroke; isolated bright grain specks are not
    _, lab, st, _ = cv2.connectedComponentsWithStats((g > 0).astype(np.uint8), connectivity=8)
    keep = np.maximum(st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT]) >= 10 * k
    keep[0] = False
    return g * keep[lab]


def antialias(mask, strength):
    """Smooth the staircase of a (mostly) binary mask into edge coverage: 4x supersampled bilinear
    contour, a little blur at that scale (up to half a pixel at strength 1), area-averaged back.
    Thin strokes survive because the contour is taken at 0.5 of a bilinear upscale."""
    if strength <= 0:
        return mask.astype(np.float32)
    h, w = mask.shape
    s = 4
    up = cv2.resize(mask.astype(np.float32), (w * s, h * s), interpolation=cv2.INTER_LINEAR)
    up = cv2.GaussianBlur(up, (0, 0), max(0.3, s * 0.5 * strength))
    aa = cv2.resize((up > 0.5).astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    return (mask * (1 - strength) + aa * strength).astype(np.float32)


def flatten_cover(cover, light_tone, dark_tone, strength):
    """Pull the tone density toward the flat light / dark tone (nearest of paper, light, dark).
    1.0 = no gradation left: every tone pixel is exactly light_tone or dark_tone. Solid is untouched."""
    if strength <= 0:
        return cover
    q = np.where(cover < light_tone / 2, 0.0,
                 np.where(cover < (light_tone + dark_tone) / 2, light_tone, dark_tone)).astype(np.float32)
    return np.where(cover < SOLID_COVER, cover * (1 - strength) + q * strength, cover).astype(np.float32)


def flatten_layers(light, dark, strength):
    """flatten_cover for finished light / dark layers whose flat levels are unknown: each layer's
    level is the median of the pixels where it dominates, the combined density snaps to the nearest
    of paper / light level / dark level, and the snapped regions get antialiased edges."""
    if strength <= 0:
        return light, dark
    levels = []
    for x, other in ((light, dark), (dark, light)):
        m = (x >= other) & (x > 0.02)
        levels.append(float(np.median(x[m])) if m.sum() > 64 else None)
    lv = sorted((v, i) for i, v in enumerate(levels) if v is not None)
    if not lv:
        return light, dark
    total = light + dark
    edges = [lv[0][0] / 2] + [(a[0] + b[0]) / 2 for a, b in zip(lv, lv[1:])] + [np.inf]
    flat = [np.zeros_like(light), np.zeros_like(dark)]
    for (v, i), lo, hi in zip(lv, edges, edges[1:]):
        flat[i] = antialias((total >= lo) & (total < hi), 1.0) * v
    light = light * (1 - strength) + flat[0] * strength
    dark = dark * (1 - strength) + flat[1] * strength
    return light.astype(np.float32), dark.astype(np.float32)


def multi_otsu(values, classes=4, bins=128):
    """Thresholds (ascending) that split values in 0..1 into `classes` groups with the largest
    between-class variance. Exhaustive over histogram edges, O(bins^2) per extra class."""
    h, _ = np.histogram(values, bins=bins, range=(0.0, 1.0))
    p = h.astype(np.float64) / max(h.sum(), 1)
    c = (np.arange(bins) + 0.5) / bins
    P = np.concatenate(([0.0], np.cumsum(p)))
    S = np.concatenate(([0.0], np.cumsum(p * c)))
    w = P[None, :] - P[:, None]                      # F[a, b]: class of bins a..b-1
    m = S[None, :] - S[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        F = np.where(w > 1e-12, m * m / w, 0.0)
    idx = np.arange(bins + 1)
    later = idx[None, :] > idx[:, None]
    # best[j]: best score of splitting bins j..end into the remaining classes, and where to cut
    best, cuts = F[:, bins].copy(), []
    for _ in range(classes - 1):
        M = np.where(later, F + best[None, :], -np.inf)
        M[:, bins] = -np.inf                         # every class keeps at least one bin
        cut = M.argmax(1)
        best = M.max(1)
        cuts.append(cut)
    t, out = 0, []
    for cut in reversed(cuts):
        t = cut[t]
        out.append(t / bins)
    return out


def auto_levels(V, ink, Lm, mass):
    """white / split / black for the tone plan: a 4-class multi-Otsu split into paper, light tone,
    dark tone and black, kept in sane bounds. The histogram takes the descreened value V away from
    the ink, plus the solid black masses at their own (blurred) value Lm: they are ink, so V has no
    black class of its own, and without them Otsu would cut the darkest grey into "black"."""
    v = np.concatenate((V[~ink], Lm[mass & ink]))
    if v.size < 1000:
        v = V.ravel()
    v = v[:: max(1, v.size // 400000)]              # a sample is plenty for a histogram
    black, split, white = multi_otsu(np.clip(v, 0, 1), 4)
    white = float(np.clip(white, 0.50, 0.95))
    black = float(np.clip(black, 0.05, min(0.60, white - 0.20)))
    split = float(np.clip(split, black + 0.05, white - 0.05))
    return white, split, black


def finish_layers(img, lines, white, black, light_tone, dark_tone, gradation, pen, pen_threshold,
                  white_lines, tone_split, lines_in_solid, smooth=True, relative=False, line_cleanup=0.0,
                  tone_flatten=0.0, line_antialias=0.0, auto=False, info=None):
    """Returns (light_tone, dark_tone, solid) as float alpha maps in 0..1 that never overlap.

    auto=True replaces white / black (and the light / dark tone split, otherwise their midpoint) with
    multi-Otsu thresholds of the image; the values used are written to `info` when it is a dict.
    smooth=False: light/dark are the printed dots, and their union with solid is the page Manga Finish prints.
    smooth=True : light/dark are the continuous tone density (flat tones + gradation) before screening,
                  split between the two layers around the split value. relative=True stretches each
                  layer so its own flat tone (light_tone / dark_tone) reaches alpha 1.
    tone_flatten pulls the tones toward flat light / dark tone (1 = no gradation).
    line_antialias smooths the solid layer's edges; the tone then reaches under the softened edge
    so the composite has no white fringe (the layers overlap there by design)."""
    L = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    k = L.shape[1] / 1024
    ink = ink_layer(L, k, line_cleanup)
    Lm = cv2.GaussianBlur(L, (0, 0), 1.5 * k)
    mass = Lm < 0.22
    strokes = ink & ~mass                                      # lines without the solid masses
    if pen is not None:
        strokes |= pen > pen_threshold
    V = value_map(L, ink, k)
    if auto:
        white, value_split, black = auto_levels(V, ink, Lm, mass)
    else:
        value_split = (white + black) / 2
    if info is not None:
        info.update(white=white, black=black, split=value_split)
    level = tone_plan(V, k, white, value_split, black)
    cover = np.take(np.float32((0.0, light_tone, dark_tone, 1.0)), level)
    if gradation:
        Vs = cv2.GaussianBlur(V, (0, 0), 3 * k)
        a = cv2.GaussianBlur(np.clip((region_spread(level, Vs) - 0.03) / 0.05, 0, 1) * gradation, (0, 0), 4 * k)
        cover = cover * (1 - a) + np.clip((white - Vs) / (white - black), 0, 1) * a
    cover = flatten_cover(cover, light_tone, dark_tone, tone_flatten)

    dots = print_screen(cover, L.shape[1] / lines)
    split = tone_split if tone_split > 0 else (light_tone + dark_tone) / 2

    beta = cv2.erode((level == 3).astype(np.uint8), np.ones((int(3 * k) | 1,) * 2, np.uint8)) > 0
    cut = np.zeros_like(beta)
    if white_lines:
        candidates = strokes if pen is None else pen > 1 - white_lines
        cut = beta & white_cut(candidates, k)

    # What Manga Finish calls "ink | strokes" is the solid layer; with lines_in_solid off only the
    # filled masses stay solid and the pen lines are dropped (use a separate line layer instead).
    black_ink = (ink | strokes) if lines_in_solid else (ink & mass)
    solid_full = (dots & (cover >= SOLID_COVER)) | black_ink | beta
    solid = solid_full & ~cut
    free = ~solid_full
    if line_antialias > 0:
        solid = antialias(solid, line_antialias)
        # the inner rim of the solid turns partly transparent: let the tone show through it, except
        # along the white cut-out lines, which must stay paper white
        ker = np.ones((3, 3), np.uint8)
        rim = solid_full & ~(cv2.erode(solid_full.astype(np.uint8), ker) > 0)
        free = free | (rim & ~(cv2.dilate(cut.astype(np.uint8), ker) > 0))
    glint = highlight_map(L, k, white) if line_cleanup else np.zeros_like(L)
    if not smooth:
        free &= glint < 0.5
        light = dots & (cover < split) & free
        dark = dots & (cover >= split) & free
        return light.astype(np.float32), dark.astype(np.float32), solid.astype(np.float32)

    # smooth density: antialias the tone plan a little, then hand each density to the light or the dark
    # layer with a soft hand-over around `split`, so the two layers add up to the original density
    dens = np.clip(cv2.GaussianBlur(cover, (0, 0), 0.8 * k), 0, 1)
    bw = max((dark_tone - light_tone) * 0.25, 0.02)
    t = np.clip((dens - (split - bw)) / (2 * bw), 0, 1)
    t = t * t * (3 - 2 * t)
    light = dens * (1 - t) * free * (1 - glint)
    dark = dens * t * free * (1 - glint)
    if relative:
        light = np.clip(light / light_tone, 0, 1)
        dark = np.clip(dark / dark_tone, 0, 1)
    return light.astype(np.float32), dark.astype(np.float32), solid.astype(np.float32)


# ---------------------------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------------------------

def pen_map(lines, i, shape):
    """White-on-black line map resized to the frame, as line strength 0..1."""
    m = lines[min(i, lines.shape[0] - 1), ..., :3].mean(-1).cpu().numpy().astype(np.float32)
    return cv2.resize(m, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def to_rgba(mask, rgb):
    """Ink alpha (bool or 0..1) -> float32 HxWx4: constant ink colour, alpha = mask (transparent elsewhere)."""
    out = np.empty(mask.shape + (4,), np.float32)
    out[..., :3] = rgb
    out[..., 3] = mask
    return torch.from_numpy(out)


class MangaFinishLayers:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "screen_lines": ("INT", {"default": 200, "min": 40, "max": 600, "step": 10,
                                     "tooltip": "画像の横幅に並べるドットの行数（ドット出力時）。120=粗い、200=標準、300=細かい。"}),
            # saved workflows store widget values by position: js/manga_tone_layers.js remaps the
            # older order (level_mode last, or missing) when such a workflow is loaded
            "level_mode": (["manual", "auto"], {"default": "auto",
                           "tooltip": "auto: 入力画像の明るさの分布を 紙・薄いトーン・濃いトーン・ベタ の4つに最もよく分ける値を multi-Otsu 法で自動で求めます（white_point / black_point は使われずグレー表示）。manual: white_point / black_point の値をそのまま使います（薄い/濃いトーンの境目はその中間）。使われた値は white_point / black_point 出力で確認できます。"}),
            "white_point": ("FLOAT", {"default": 0.80, "min": 0.3, "max": 1.0, "step": 0.01,
                                      "tooltip": "これより明るい部分は紙（白）になり、どのレイヤーにも入りません。"}),
            "black_point": ("FLOAT", {"default": 0.30, "min": 0.0, "max": 0.7, "step": 0.01,
                                      "tooltip": "これより暗い部分はベタレイヤーになります。上げるとベタが増えます。"}),
            "light_tone": ("FLOAT", {"default": 0.18, "min": 0.02, "max": 0.9, "step": 0.01,
                                     "tooltip": "フラットな薄いトーンの濃度（ドット面積率）。"}),
            "dark_tone": ("FLOAT", {"default": 0.42, "min": 0.02, "max": 0.95, "step": 0.01,
                                    "tooltip": "フラットな濃いトーンの濃度（ドット面積率）。"}),
            "gradation": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                    "tooltip": "元画像が滑らかに変化する部分のグラデーションの強さ。グラデーションは tone_split を境に薄いトーンと濃いトーンのレイヤーへ振り分けられます。0 = フラットなトーンのみ。"}),
            "tone_split": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 0.95, "step": 0.01,
                                     "tooltip": "グラデーションを薄いトーンのレイヤーから濃いトーンのレイヤーへ切り替える濃度のしきい値。0 = 自動（light_tone と dark_tone の中間）。"}),
            "pen_threshold": ("FLOAT", {"default": 0.30, "min": 0.05, "max": 0.95, "step": 0.05,
                                        "tooltip": "pen_lines を接続したときのみ有効。下げるほど多くの線を描き込みます。"}),
            "white_lines": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 1.0, "step": 0.05,
                                      "tooltip": "ベタの中に白抜き線を入れます（透明になります）。大きいほど多くの線を残し、0 でオフ。"}),
            "tone_output": (["gradation (smooth)", "screentone (dots)"], {"default": "gradation (smooth)",
                           "tooltip": "gradation (smooth): 薄い/濃いトーンのレイヤーを滑らかな濃淡マスク（アルファ＝トーンの濃さ）で出力。ドットなし。screentone (dots): Manga Finish と同じ網点で出力。"}),
            "mask_scale": (["density", "tone relative"], {"default": "density",
                           "tooltip": "滑らか出力のときのみ有効。density: アルファが実際のトーンの濃さ（薄トーン約0.18、濃トーン約0.42）。tone relative: 各レイヤー自身のトーンがアルファ1になるよう伸ばします。自分のスクリーントーン用マスクに便利。"}),
            "lines_in_solid": ("BOOLEAN", {"default": True,
                                           "tooltip": "ON: ペン線・墨線もベタレイヤーに含めます。OFF: ベタレイヤーは黒く塗りつぶした面だけになります。"}),
            "ink_color": ("STRING", {"default": "#000000", "tooltip": "3つのレイヤー共通のインク色（16進数）。背景は常に透明です。"}),
            "line_cleanup": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 1.0, "step": 0.05,
                                       "tooltip": "線検出のノイズ除去。グレーの粒子やハイライトの縁が線として拾われ、線の周りがガビガビになるのを防ぎます。上げるほど細くはっきり暗い線だけを残します（薄いハッチングが消える場合は下げる）。0 = Manga Finish と同じ。"}),
            "tone_flatten": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                       "tooltip": "トーンの単調化。グラデーションを薄いトーン / 濃いトーンのフラットな濃度へ寄せます。1.0 = グラデーション無しの完全なフラット、0 = そのまま。"}),
            "line_antialias": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                         "tooltip": "solid（線画・ベタ）出力のアンチエイリアス強度。0 = 2値（ジャギーあり）、1.0 = 最大。トーンは線の縁の下まで入るので白い縁は出ません。"}),
        }, "optional": {
            "pen_lines": ("IMAGE", {"tooltip": "ベタレイヤーに加える線画マップ（例: Manga Lineart の出力）。"}),
        }}

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "FLOAT", "FLOAT")
    RETURN_NAMES = ("light_tone", "dark_tone", "solid", "white_point", "black_point")
    OUTPUT_TOOLTIPS = ("", "", "", "実際に使われた white_point（auto では自動で求めた値、バッチでは1枚目）。",
                       "実際に使われた black_point（auto では自動で求めた値、バッチでは1枚目）。")
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, image, screen_lines, white_point, black_point, light_tone, dark_tone, gradation,
              tone_split, pen_threshold, white_lines, tone_output, mask_scale, lines_in_solid, ink_color,
              line_cleanup=0.4, tone_flatten=0.0, line_antialias=0.0, level_mode="auto", pen_lines=None):
        rgb = parse_color(ink_color)
        auto = level_mode.startswith("auto")
        outs = ([], [], [])
        used = []
        for i, frame in enumerate(image):
            img = (frame[..., :3].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            pen = None if pen_lines is None else pen_map(pen_lines, i, img.shape[:2])
            info = {}
            masks = finish_layers(img, screen_lines, white_point, black_point, light_tone, dark_tone,
                                  gradation, pen, pen_threshold, white_lines, tone_split, lines_in_solid,
                                  smooth=tone_output.startswith("gradation"), relative=mask_scale == "tone relative", line_cleanup=line_cleanup,
                                  tone_flatten=tone_flatten, line_antialias=line_antialias,
                                  auto=auto, info=info)
            used.append(info)
            if auto:
                print(f"[MangaFinishLayers] frame {i}: auto white_point {info['white']:.3f}, "
                      f"black_point {info['black']:.3f} (tone split {info['split']:.3f})")
            for bucket, mask in zip(outs, masks):
                bucket.append(to_rgba(mask, rgb))
        layers = tuple(torch.stack(b).to(image.device) for b in outs)
        return layers + (float(used[0]["white"]), float(used[0]["black"]))


def parse_color(text):
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    try:
        return np.float32([int(t[i:i + 2], 16) for i in (0, 2, 4)]) / 255
    except (ValueError, IndexError):
        return np.float32((0, 0, 0))


# ---------------------------------------------------------------------------------------------
# Solid refine: an edit model (e.g. Qwen Image) redraws the solid layer as clean line art + beta;
# whatever was solid before and is gone now is handed back to the tone layers.
# ---------------------------------------------------------------------------------------------

def layer_alpha(images, i, shape, drawing=False):
    """Ink amount 0..1 of frame i, resized to shape. RGBA layers give their alpha; an RGB image is
    read as ink on white paper (1 - luminance). drawing=True always reads the drawing by luminance
    (over white where it is transparent): some VAEs decode to RGBA with an all-opaque alpha."""
    f = images[min(i, images.shape[0] - 1)].cpu().numpy().astype(np.float32)
    dark = 1 - f[..., :3] @ np.float32((0.299, 0.587, 0.114))
    if f.shape[-1] != 4:
        a = dark
    else:
        a = dark * f[..., 3] if drawing else f[..., 3]
    if a.shape != shape:
        shrink = a.shape[0] > shape[0]
        a = cv2.resize(a, (shape[1], shape[0]), interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)
    return np.clip(a, 0, 1)


def clean_solid(ink, threshold, softness, despeckle, k):
    """Edited image (ink 0..1) -> solid alpha: thresholded, optionally antialiased, with lone specks
    removed and pinholes inside the beta filled."""
    if softness > 0:
        a = np.clip((ink - (1 - threshold) + softness / 2) / softness, 0, 1)
    else:
        a = (ink > 1 - threshold).astype(np.float32)
    if despeckle:
        limit = despeckle * k * k
        for value in (1, 0):        # black specks on paper, then white pinholes in the beta
            region = (a > 0.5) == bool(value)
            _, lab, st, _ = cv2.connectedComponentsWithStats(region.astype(np.uint8), connectivity=8)
            small = st[:, cv2.CC_STAT_AREA] < limit
            small[0] = False
            a = np.where(small[lab], 1 - value, a).astype(np.float32)
    return a


def fill_from_surroundings(a, unknown, k):
    """Values of a inside `unknown`, continued smoothly from the known pixels around it."""
    if not unknown.any():
        return a
    # 16-bit: OpenCV's float32 inpaint returns garbage (values far outside 0..1)
    f = cv2.inpaint((np.clip(a, 0, 1) * 65535).astype(np.uint16), unknown.astype(np.uint8),
                    max(3.0, 3 * k), cv2.INPAINT_TELEA).astype(np.float32) / 65535
    return np.where(unknown, cv2.GaussianBlur(f, (0, 0), 2 * k), a).astype(np.float32)


def refine_layers(light, dark, before, edited, threshold, softness, despeckle, tone_fill, fill_floor,
                  tone_flatten=0.0, line_antialias=0.0, screen_lines=0):
    """light/dark/before: alphas from Manga Finish Layers; edited: ink of the redrawn solid layer.
    Returns new (light, dark, solid) and the lost mask (solid before, not solid after).
    screen_lines > 0 prints the finished tone density as screentone: one page-aligned 45 degree
    screen for both layers (no moire between them), each dot going to the layer that dominates there."""
    k = before.shape[1] / 1024
    solid = antialias(clean_solid(edited, threshold, softness, despeckle, k), line_antialias)
    was = before > 0.5
    lost = was & (solid < 0.5)
    # tone under the old solid was cut out (alpha 0), so the whole old solid is unknown, not zero
    fl = fill_from_surroundings(light, was, k)
    fd = fill_from_surroundings(dark, was, k)
    if fill_floor > 0:
        # a vanished beta mass (not a thin sliver beside a moved line) keeps at least this density
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(7 * k) | 1,) * 2)
        mass = cv2.GaussianBlur(cv2.morphologyEx(lost.astype(np.uint8), cv2.MORPH_OPEN, ker).astype(np.float32), (0, 0), k)
        deficit = np.clip(fill_floor - fl - fd, 0, 1) * mass
        to_light = fl > fd
        fl = fl + deficit * to_light
        fd = fd + deficit * ~to_light
    # the tone runs on under the new solid, so a soft solid edge composites onto tone instead of a
    # white fringe; it is dropped only where the solid is fully opaque
    weight = np.where(lost, tone_fill, 1.0)
    light = np.where(was, fl, light) * weight
    dark = np.where(was, fd, dark) * weight
    light, dark = flatten_layers(light, dark, tone_flatten)
    if screen_lines:
        dots = print_screen(np.clip(light + dark, 0, 1), before.shape[1] / screen_lines)
        is_light = light >= dark
        light = (dots & is_light).astype(np.float32)
        dark = (dots & ~is_light).astype(np.float32)
    covered = solid >= 0.999
    light = np.where(covered, 0, light)
    dark = np.where(covered, 0, dark)
    return light.astype(np.float32), dark.astype(np.float32), solid, lost.astype(np.float32)


class MangaSolidRefine:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "light_tone": ("IMAGE", {"tooltip": "Manga Finish Layers の light_tone 出力（gradation (smooth) モード推奨）。"}),
            "dark_tone": ("IMAGE", {"tooltip": "Manga Finish Layers の dark_tone 出力。"}),
            "solid": ("IMAGE", {"tooltip": "Manga Finish Layers の solid 出力（修正前）。"}),
            "refined": ("IMAGE", {"tooltip": "solid を Qwen Image などで線画＋黒ベタに描き直した画像（白地に黒）。サイズが違っても solid に合わせて拡大縮小します。"}),
            "threshold": ("FLOAT", {"default": 0.5, "min": 0.05, "max": 0.95, "step": 0.01,
                                    "tooltip": "refined のこの明るさより暗い部分をベタ（黒）とみなします。上げるほど黒が増えます。"}),
            "edge_softness": ("FLOAT", {"default": 0.20, "min": 0.0, "max": 0.5, "step": 0.01,
                                        "tooltip": "solid の輪郭のアンチエイリアス幅。0 = 完全な2値（ジャギーあり）。"}),
            "despeckle": ("INT", {"default": 12, "min": 0, "max": 200,
                                  "tooltip": "これより小さい（1024px幅換算の面積）黒い点を消し、ベタの中の白い穴を埋めます。0 = オフ。"}),
            "tone_fill": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                    "tooltip": "修正前 solid で黒だったのに修正後に黒でなくなった部分を、周囲のグラデーションで補完する強さ。0 = 補完しない（白く抜ける）。"}),
            "fill_floor": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "消えたベタの塊（線の脇の細いズレは除く）に入れるトーンの最低濃度。周囲が白で補完すると消えてしまう黒い塊を、トーンとして残したいときに上げます。"}),
            "ink_color": ("STRING", {"default": "#000000", "tooltip": "出力レイヤーのインク色（16進数）。"}),
            "tone_flatten": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.05,
                                       "tooltip": "トーンの単調化。グラデーションを各トーンのフラットな濃度（そのレイヤーで最も多い濃さ）へ寄せます。1.0 = グラデーション無しの完全なフラット、0 = そのまま。"}),
            "line_antialias": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05,
                                         "tooltip": "最終 solid（線画・ベタ）のアンチエイリアス強度。0 = edge_softness の結果のまま、1.0 = 最大。トーンは線の縁の下まで入るので白い縁は出ません。"}),
            "tone_output": (["gradation (smooth)", "screentone (dots)"], {"default": "screentone (dots)",
                           "tooltip": "gradation (smooth): 薄い/濃いトーンを滑らかな濃淡マスクで出力。screentone (dots): 補完・単調化を済ませた濃度を 45° の網点トーンにして出力（2つのレイヤーは同じ網目なのでモアレなし）。入力の Manga Finish Layers は gradation (smooth) / density のままにしてください。"}),
            "screen_lines": ("INT", {"default": 250, "min": 40, "max": 600, "step": 10,
                                     "tooltip": "screentone (dots) のとき、画像の横幅に並べる網点の行数。120=粗い、200=標準、300=細かい。"}),
        }}

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = ("light_tone", "dark_tone", "solid")
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, light_tone, dark_tone, solid, refined, threshold, edge_softness, despeckle, tone_fill,
              fill_floor, ink_color, tone_flatten=0.7, line_antialias=0.5,
              tone_output="screentone (dots)", screen_lines=250):
        rgb = parse_color(ink_color)
        outs = ([], [], [])
        for i in range(solid.shape[0]):
            shape = tuple(solid.shape[1:3])
            res = refine_layers(layer_alpha(light_tone, i, shape), layer_alpha(dark_tone, i, shape),
                                layer_alpha(solid, i, shape), layer_alpha(refined, i, shape, drawing=True),
                                threshold, edge_softness, despeckle, tone_fill, fill_floor,
                                tone_flatten, line_antialias,
                                screen_lines if tone_output.startswith("screentone") else 0)
            for bucket, a in zip(outs, res[:3]):
                bucket.append(to_rgba(a, rgb))
        return tuple(torch.stack(b).to(solid.device) for b in outs)


class MangaLayerFlatten:
    """Stacks RGBA layers onto paper as a plain RGB image: the solid layer as an edit model input,
    or all three layers as a preview of the page."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "layer_1": ("IMAGE", {"tooltip": "一番下に重ねるレイヤー（RGBA）。"}),
            "paper_color": ("STRING", {"default": "#ffffff", "tooltip": "下地の色（16進数）。"}),
        }, "optional": {
            "layer_2": ("IMAGE", {"tooltip": "layer_1 の上に重ねるレイヤー（RGBA）。"}),
            "layer_3": ("IMAGE", {"tooltip": "一番上に重ねるレイヤー（RGBA）。"}),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, layer_1, paper_color, layer_2=None, layer_3=None):
        out = parse_color(paper_color).copy()
        page = torch.from_numpy(out).to(layer_1.device).expand(*layer_1.shape[:3], 3).clone()
        for layer in (layer_1, layer_2, layer_3):
            if layer is None:
                continue
            if layer.shape[1:3] != page.shape[1:3]:
                layer = torch.nn.functional.interpolate(layer.movedim(-1, 1), size=tuple(page.shape[1:3]),
                                                        mode="bilinear", align_corners=False).movedim(1, -1)
            a = layer[..., 3:4] if layer.shape[-1] == 4 else torch.ones_like(layer[..., :1])
            page = page * (1 - a) + layer[..., :3] * a
        return (page.clamp(0, 1),)


NODE_CLASS_MAPPINGS = {"MangaFinishLayers": MangaFinishLayers, "MangaSolidRefine": MangaSolidRefine,
                       "MangaLayerFlatten": MangaLayerFlatten}
NODE_DISPLAY_NAME_MAPPINGS = {"MangaFinishLayers": "Manga Finish Layers (Tone / Solid RGBA)",
                              "MangaSolidRefine": "Manga Solid Refine (Tone Fill)",
                              "MangaLayerFlatten": "Manga Layer Flatten"}
