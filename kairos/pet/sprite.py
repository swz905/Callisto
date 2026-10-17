"""Pixel art for the desktop pet: a little CRT-headed robot with a curly antenna, stubby arms and a glowing face.

All data, no Tk (testable). Shapes are rasterised into a material map, then every material gets an automatic
1-pixel outline and top-left lighting (highlight / light / base / shadow), like hand-shaded pixel art. The screen
is a little CRT: tinted background, scanlines, a glare streak and a soft glow around whatever is lit.

    frame(mood, t, look=(0, 0), skin="slate") -> {(x, y): "#rrggbb"}

`look` is where the mouse is (-1..1 on each axis): the eyes follow it.
"""

from __future__ import annotations

import math
import random

W, H = 48, 66
SCALE = 3

SKINS = {  # material -> (shadow, base, light, highlight)
    "slate": {"case": ("#3a3f5c", "#59618f", "#7a84b6", "#a6b0e2"), "metal": ("#272a3a", "#41475f", "#5d6584", "#868fb2"),
              "bezel": ("#121521", "#1b1f30", "#252a40", "#30364f"), "ball": ("#b04a63", "#e0607e", "#ff8aa3", "#ffc2cf"),
              "outline": "#0c0d16"},
    "classic": {"case": ("#24262e", "#363946", "#4b4f60", "#676c80"), "metal": ("#1a1b21", "#2b2d36", "#3d4050", "#5a5f73"),
                "bezel": ("#0d0d12", "#15151c", "#1d1d26", "#262632"), "ball": ("#1a1b21", "#2b2d36", "#3d4050", "#5a5f73"),
                "outline": "#07070b"},
    "mint": {"case": ("#3f7a6c", "#5fae99", "#86d1bb", "#b8eedd"), "metal": ("#2c4f48", "#3f6d63", "#56907f", "#79b3a1"),
             "bezel": ("#10201d", "#172b27", "#1f3833", "#284640"), "ball": ("#b08a2c", "#e6b84a", "#ffd76e", "#fff0b3"),
             "outline": "#0a1412"},
    "peach": {"case": ("#a8615a", "#d98a7c", "#f2ae9b", "#ffd5c6"), "metal": ("#6b4448", "#8c5a5c", "#ad7473", "#c99490"),
              "bezel": ("#21141a", "#2c1b22", "#38232c", "#462c37"), "ball": ("#3f6f8f", "#5a9cc4", "#7fc0e6", "#bfe6ff"),
              "outline": "#160b0f"},
}
FACE = {  # screen colour per mood
    "idle": "#5ef0a0", "happy": "#ff8fd8", "listening": "#5cc8ff", "thinking": "#ffd166", "speaking": "#5ef0a0",
    "alert": "#ff5a5a", "offline": "#ff4545", "sleep": "#8090c0",
}
MOODS = tuple(FACE)
BLUSH = "#ff6fae"
WHITE = "#ffffff"

# screen rectangle (inside the bezel)
SX0, SY0, SX1, SY1 = 10, 25, 37, 39
SCREEN_W, SCREEN_H = SX1 - SX0 + 1, SY1 - SY0 + 1


# --- colour helpers ------------------------------------------------------------------------------
def _rgb(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def mix(a: str, b: str, k: float) -> str:
    """k=0 -> a, k=1 -> b."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * k) for x, y in zip(ra, rb))


# --- rasterising ---------------------------------------------------------------------------------
def round_rect(x0: int, y0: int, x1: int, y1: int, r: int) -> set[tuple[int, int]]:
    pts = set()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cx = x0 + r if x < x0 + r else x1 - r if x > x1 - r else x
            cy = y0 + r if y < y0 + r else y1 - r if y > y1 - r else y
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + r * 0.6:
                pts.add((x, y))
    return pts


def ellipse(cx: float, cy: float, rx: float, ry: float) -> set[tuple[int, int]]:
    return {(x, y) for y in range(math.floor(cy - ry), math.ceil(cy + ry) + 1)
            for x in range(math.floor(cx - rx), math.ceil(cx + rx) + 1)
            if ((x - cx) / max(rx, .1)) ** 2 + ((y - cy) / max(ry, .1)) ** 2 <= 1.0}


def line(points: list[tuple[float, float]], thick: int = 1) -> set[tuple[int, int]]:
    out = set()
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
        for i in range(steps + 1):
            x, y = x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps
            for dx in range(thick):
                for dy in range(thick):
                    out.add((round(x) + dx, round(y) + dy))
    return out


def shade(mats: dict, palette: dict) -> dict:
    """Material map -> colours: automatic outline + top-left light."""
    px = {}
    for (x, y), m in mats.items():
        shadow, base, light, hi = palette[m]
        up, left = mats.get((x, y - 1)) != m, mats.get((x - 1, y)) != m
        down, right = mats.get((x, y + 1)) != m, mats.get((x + 1, y)) != m
        if up and left:
            c = hi
        elif up or left:
            c = light
        elif down or right or (m == "case" and mats.get((x, y + 2)) != m):
            c = shadow
        else:
            c = base
        px[(x, y)] = c
    outline = palette["outline"]
    for (x, y) in list(mats):
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n not in mats and n not in px:
                px[n] = outline
    return px


def outlined(pixels: dict, colour: str) -> dict:
    """Give free-floating effect pixels (hearts, z's, '!') a dark outline so they read on any wallpaper."""
    out = dict(pixels)
    for (x, y) in pixels:
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n not in pixels:
                out.setdefault(n, colour)
    return out


# --- the robot -----------------------------------------------------------------------------------------
def _arm_pose(mood: str, t: float) -> tuple[int, int]:
    """(left raise, right raise) in pixels."""
    if mood == "alert":
        return 4, 4  # hands up
    if mood == "happy":
        return (4 if int(t * 5) % 2 else 1), 0  # waving
    if mood == "speaking":
        return 0, (2 if int(t * 3) % 2 else 0)  # little gesture
    return 0, 0


def body(mood: str, t: float, skin: str) -> dict:
    mats: dict = {}

    def put(points, m):
        for p in points:
            mats[p] = m

    # antenna: a stem that curls into a hook with a ball, swaying a little at the top
    sway = round(math.sin(t * 2.2) * (1.4 if mood in ("happy", "alert") else 0.8))
    stem = [(23.5, 19), (23.5, 15), (22.5, 13), (21 + sway * .5, 11), (21 + sway * .7, 8.5), (22.5 + sway, 6.5),
            (25 + sway, 5.5), (27.5 + sway, 6)]
    put(line(stem, 2), "metal")
    put(ellipse(30.5 + sway, 9, 3, 3), "ball")
    # ears / side knobs, neck, feet
    put(round_rect(1, 28, 5, 36, 1), "metal")
    put(round_rect(42, 28, 46, 36, 1), "metal")
    put(round_rect(20, 45, 27, 48, 0), "metal")
    put(round_rect(14, 59, 22, 62, 2), "metal")
    put(round_rect(25, 59, 33, 62, 2), "metal")
    lr, rr = _arm_pose(mood, t)
    arms = [round_rect(8 - lr // 2, 49 - lr * 2, 12 - lr // 2, 56 - lr * 2, 2),  # raised arms swing outwards
            round_rect(35 + rr // 2, 49 - rr * 2, 39 + rr // 2, 56 - rr * 2, 2)]
    # body + belly panel
    put(round_rect(14, 47, 33, 59, 5), "case")
    put(round_rect(18, 50, 29, 56, 2), "bezel")
    # head
    put(round_rect(4, 19, 43, 45, 7), "case")
    put(round_rect(8, 23, 39, 43, 5), "bezel")
    for arm in arms:  # in front, so raised hands show against the head
        put(arm, "metal")
    palette = dict(SKINS.get(skin, SKINS["slate"]))
    if mood in ("listening", "alert") and int(t * 4) % 2 == 0:  # the antenna ball lights up
        c = FACE[mood]
        palette["ball"] = (mix(c, "#000000", .35), c, mix(c, WHITE, .3), mix(c, WHITE, .7))
    px = shade(mats, palette)
    for p in ((8, 21), (9, 21), (10, 21), (7, 22)):  # a specular glint on the head's top-left corner
        px[p] = mix(palette["case"][3], WHITE, .45)
    return px


# --- the face on the screen ------------------------------------------------------------------------------
def _eye(cx: int, top: int, w: int = 5, h: int = 6) -> set[tuple[int, int]]:
    pts = {(cx + x, top + y) for x in range(w) for y in range(h)}
    for corner in ((cx, top), (cx + w - 1, top), (cx, top + h - 1), (cx + w - 1, top + h - 1)):
        pts.discard(corner)
    return pts


_FONT = {  # 5-row glyphs for screen text and effects
    "N": ["#..#", "##.#", "#.##", "#..#", "#..#"], "U": ["#..#", "#..#", "#..#", "#..#", ".##."],
    "L": ["#...", "#...", "#...", "#...", "####"], "?": ["###", "..#", ".##", "...", ".#."],
    "!": ["#", "#", "#", ".", "#"], "Z": ["####", "..#.", ".#..", "#...", "####"], "z": ["###", ".#.", "###"],
}


def text_bitmap(text: str) -> list[str]:
    """Text in the 5-row font, centred on the screen: SCREEN_H rows of SCREEN_W characters ('#' = lit)."""
    cols = [_FONT.get(ch, _FONT["?"]) for ch in text[:5]]
    rows = [".".join(c[r] for c in cols) for r in range(5)]
    left, top = max(0, (SCREEN_W - len(rows[0])) // 2), (SCREEN_H - 5) // 2
    grid = ["." * SCREEN_W for _ in range(SCREEN_H)]
    for r, row in enumerate(rows):
        grid[top + r] = ("." * left + row + "." * SCREEN_W)[:SCREEN_W]
    return grid


def face_pixels(mood: str, t: float, look: tuple[float, float] = (0, 0)) -> tuple[dict, set]:
    """Screen-relative lit pixels -> colour, plus the set of 'eye shine' pixels."""
    col = FACE.get(mood, FACE["idle"])
    lit: dict = {}
    shine: set = set()
    dx = max(-1, min(1, round(look[0])))
    dy = max(-1, min(1, round(look[1])))
    blink = mood in ("idle", "speaking", "listening") and (t % 4.2) < 0.14

    def paint(points, c=col):
        for p in points:
            if 0 <= p[0] < SCREEN_W and 0 <= p[1] < SCREEN_H:
                lit[p] = c

    lx, rx, ey = 6 + dx, 17 + dx, 3 + dy
    if mood == "offline":
        for r, row in enumerate(text_bitmap("NULL")):
            for c, ch in enumerate(row):
                if ch == "#" and not (int(t * 8) % 9 == 0 and r % 2):  # occasional flicker
                    lit[(c, r)] = col
        rnd = random.Random(int(t * 10))
        for _ in range(14):  # static
            lit.setdefault((rnd.randrange(SCREEN_W), rnd.randrange(SCREEN_H)), mix(col, "#000000", .55))
        return lit, shine
    if mood == "sleep" or blink:
        for x0 in (lx, rx):  # closed eyes: a little smile-curve
            paint([(x0, ey + 3), (x0 + 1, ey + 4), (x0 + 2, ey + 4), (x0 + 3, ey + 4), (x0 + 4, ey + 3)])
    elif mood == "happy":
        for x0 in (lx, rx):  # ^ ^
            paint(line([(x0, ey + 5), (x0 + 2, ey + 2), (x0 + 4, ey + 5)], 1))
            paint(line([(x0, ey + 6), (x0 + 2, ey + 3), (x0 + 4, ey + 6)], 1))
    elif mood == "alert":
        for x0 in (lx, rx):
            paint(_eye(x0, ey - 1, 5, 7))
            shine.update({(x0 + 1, ey), (x0 + 1, ey + 1)})
    else:
        top_cut = 2 if mood == "thinking" else 0  # half-lidded while thinking
        for x0 in (lx, rx):
            paint({p for p in _eye(x0, ey) if p[1] >= ey + top_cut})
            shine.add((x0 + 1, ey + 1 + top_cut))
    m = 11  # mouth row
    if mood == "speaking":
        h = [1, 3, 2, 3, 1, 2][int(t * 9) % 6]
        mouth = round_rect(11, m - 1, 16, m - 1 + h, 1) if h > 1 else {(x, m) for x in range(11, 17)}
        inner = round_rect(12, m, 15, m - 2 + h, 0) if h > 2 else set()
        paint(mouth - inner)
    elif mood == "happy":
        paint({(x, m - 1) for x in range(10, 18)} | {(x, m) for x in range(11, 17)} | {(x, m + 1) for x in range(12, 16)})
    elif mood in ("alert", "listening"):
        r = 1.6 if mood == "alert" else 1.1
        paint(ellipse(13.5, m, r + .4, r + .4) - ellipse(13.5, m, r - .6, r - .6))
    elif mood == "thinking":
        paint({(x, m) for x in range(12, 16)} | {(16, m - 1)})
    elif mood == "sleep":
        paint({(13, m), (14, m)})
    else:  # idle: a little cat mouth "w"
        paint([(10, m - 1), (11, m), (12, m), (13, m - 1), (14, m - 1), (15, m), (16, m), (17, m - 1)])
    if mood in ("idle", "happy", "speaking", "listening"):
        for x0 in (lx - 3, rx + 4):  # blush
            for p in ((x0 + 1, ey + 7), (x0 + 2, ey + 7)):
                if p not in lit and 0 <= p[0] < SCREEN_W:
                    lit[p] = mix(BLUSH, "#000000", .25)
    return lit, shine


def screen(mood: str, t: float, look=(0, 0)) -> dict:
    """The CRT: tinted dark background, scanlines, glow around lit pixels, glare streak, face on top."""
    col = FACE.get(mood, FACE["idle"])
    dim = .55 if mood == "sleep" else 1.0
    bg = mix("#06080d", col, .07)
    lit, shine = face_pixels(mood, t, look)
    area = round_rect(0, 0, SCREEN_W - 1, SCREEN_H - 1, 3)
    out = {}
    for (x, y) in area:
        c = bg
        if y % 2:  # scanlines
            c = mix(c, "#000000", .35)
        if min(x, y, SCREEN_W - 1 - x, SCREEN_H - 1 - y) == 0:  # vignette
            c = mix(c, "#000000", .4)
        if any((x + i, y + j) in lit for i in (-1, 0, 1) for j in (-1, 0, 1)):  # bloom
            c = mix(c, col, .22 * dim)
        if 2 <= x + y <= 3 or (x + y == 5 and x < 6):  # glare streak, top-left
            c = mix(c, WHITE, .16)
        out[(x, y)] = c
    for p, c in lit.items():
        if p in area:
            out[p] = mix("#000000", c, dim) if dim < 1 else c
    for p in shine:
        if p in out:
            out[p] = mix(out[p], WHITE, .75)
    return {(SX0 + x, SY0 + y): c for (x, y), c in out.items()}


# --- effects around the robot ------------------------------------------------------------------------------
HEART = [".#.#.", "#####", "#####", ".###.", "..#.."]
SPARK = ["..#..", ".###.", "#####", ".###.", "..#.."]


def _sprite(rows: list[str], x0: int, y0: int, colour: str) -> dict:
    return {(x0 + c, y0 + r): colour for r, row in enumerate(rows) for c, ch in enumerate(row) if ch == "#"}


def effects(mood: str, t: float, skin: str) -> dict:
    ol = SKINS.get(skin, SKINS["slate"])["outline"]
    col = FACE.get(mood, FACE["idle"])
    fx: dict = {}
    if mood == "happy":  # hearts float up and fade
        for i, x in enumerate((38, 5)):
            phase = (t * .8 + i * .5) % 1.0
            if phase < .85:
                fx.update(outlined(_sprite(HEART, x, int(16 - phase * 14), mix(BLUSH, WHITE, phase * .4)), ol))
    elif mood == "sleep":  # z Z drifting up
        for i, glyph in enumerate(("z", "Z")):
            phase = (t * .5 + i * .5) % 1.0
            fx.update(outlined(_sprite(_FONT[glyph], 34 + i * 5, int(14 - phase * 12),
                                       mix(FACE["sleep"], WHITE, .3)), ol))
    elif mood == "alert":  # flashing "!"s
        if int(t * 4) % 2 == 0:
            fx.update(outlined(_sprite(_FONT["!"], 41, 4, FACE["alert"]), ol))
            fx.update(outlined(_sprite(_FONT["!"], 6, 6, FACE["alert"]), ol))
    elif mood == "thinking":  # thought dots rising to a "?"
        n = int(t * 3) % 4
        for i in range(n):
            x, y = 35 + i * 3, 15 - i * 4
            fx.update(outlined({(x, y): col, (x + 1, y): col, (x, y + 1): col, (x + 1, y + 1): col}, ol))
        if n == 3:
            fx.update(outlined(_sprite(_FONT["?"], 43, 1, col), ol))
    elif mood == "listening":  # sound waves on both sides, pulsing outwards
        k = int(t * 5) % 3
        for i in range(k + 1):
            r = 3 + i * 3
            for a in range(-3, 4):
                d = round(math.sqrt(max(0, r * r - a * a)) * .7)
                fx[(44 + d - 1, 32 + a)] = mix(col, WHITE, .2)
                fx[(3 - d, 32 + a)] = mix(col, WHITE, .2)
    elif mood == "speaking":  # a little sparkle
        if int(t * 2) % 2:
            fx.update(outlined(_sprite(SPARK, 41, 12, mix(col, WHITE, .4)), ol))
    return fx


def chest_light(mood: str, t: float) -> dict:
    col = FACE.get(mood, FACE["idle"])
    pulse = .5 + .5 * math.sin(t * (6 if mood in ("listening", "speaking", "alert") else 2))
    c = mix(mix(col, "#000000", .55), col, pulse) if mood != "offline" else mix(col, "#000000", .5)
    out = {(x, y): c for x, y in ((23, 52), (24, 52), (23, 53), (24, 53))}
    out[(23, 52)] = mix(c, WHITE, .5)
    for p in ((21, 55), (22, 55), (25, 55), (26, 55)):  # vents
        out[p] = "#3b4060"
    return out


def shadow() -> dict:
    """A dithered ground shadow (works with colour-key transparency: no alpha needed)."""
    return {(x, y): "#0b0c12" for (x, y) in ellipse(24, 64.2, 14, 1.4) if (x + y) % 2 == 0}


# --- one animation frame ------------------------------------------------------------------------------------
def bob_offset(mood: str, t: float) -> int:
    """Whole-robot hop in pixels (0 = on the ground)."""
    if mood in ("sleep", "offline"):
        return 0
    if mood in ("happy", "alert"):
        return 2 if math.sin(t * 7) > .3 else 0
    return 1 if math.sin(t * 1.7) > .5 else 0


def frame(mood: str, t: float, look: tuple[float, float] = (0, 0), skin: str = "slate") -> dict[tuple[int, int], str]:
    """Every pixel of one frame. The robot hops; its shadow stays on the ground."""
    mood = mood if mood in FACE else "idle"
    dy = bob_offset(mood, t)
    robot = body(mood, t, skin)
    robot.update(screen(mood, t, look))
    robot.update(chest_light(mood, t))
    px = dict(shadow())
    px.update({(x, y - dy): c for (x, y), c in robot.items()})
    px.update(effects(mood, t, skin))
    return {k: v for k, v in px.items() if 0 <= k[0] < W and 0 <= k[1] < H}


def to_rows(px: dict, empty: str) -> list[list[str]]:
    """Pixel dict -> H rows of W colours (for Tk PhotoImage.put)."""
    rows = [[empty] * W for _ in range(H)]
    for (x, y), c in px.items():
        rows[y][x] = c
    return rows
