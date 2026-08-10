"""
cogs/battle/render.py
────────────────────────
Battle scene image rendering (Pillow) — background presets, sprite
fetching, and the HP-bar overlay.
"""

import io
import random
from typing import Optional, TYPE_CHECKING

import aiohttp
import discord

from .constants import PIL_OK, LEVEL, CANVAS_W, CANVAS_H, SPRITE_SCALE

if PIL_OK:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

if TYPE_CHECKING:
    from .engine import BattlePokemon


BACKGROUNDS = [
    dict(name="Spring Day",   sky=(135, 206, 235, 255), ground=(140, 200, 90, 255),
         patch=(160, 215, 110, 255), time="day",   season="spring"),
    dict(name="Spring Night", sky=(28, 38, 78, 255),    ground=(60, 85, 55, 255),
         patch=(75, 100, 68, 255),  time="night", season="spring"),
    dict(name="Summer Day",   sky=(90, 175, 240, 255),  ground=(120, 190, 70, 255),
         patch=(140, 205, 90, 255), time="day",   season="summer"),
    dict(name="Summer Night", sky=(18, 28, 66, 255),    ground=(45, 75, 48, 255),
         patch=(58, 90, 58, 255),   time="night", season="summer"),
    dict(name="Autumn Day",   sky=(180, 190, 210, 255), ground=(190, 140, 70, 255),
         patch=(205, 155, 85, 255), time="day",   season="autumn"),
    dict(name="Autumn Night", sky=(32, 32, 52, 255),    ground=(85, 65, 42, 255),
         patch=(98, 78, 52, 255),   time="night", season="autumn"),
    dict(name="Winter Day",   sky=(205, 222, 235, 255), ground=(232, 238, 245, 255),
         patch=(245, 248, 252, 255), time="day",  season="winter"),
    dict(name="Winter Night", sky=(12, 18, 42, 255),    ground=(195, 202, 215, 255),
         patch=(212, 218, 228, 255), time="night", season="winter"),
]


def pick_background() -> dict:
    """Roll a random season/time-of-day background preset for a battle.
    Stamps a fresh random seed onto the copy so the scattered decorations
    (stars/snow/leaves/flowers) are stable for every turn of *this* battle
    but still vary from the next battle that rolls the same preset."""
    preset = dict(random.choice(BACKGROUNDS))
    preset["seed"] = random.randint(0, 1_000_000_000)
    return preset


_FONT_CACHE: dict = {}


_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = True):
    if not PIL_OK:
        return None
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                 ("Arial Bold.ttf" if bold else "Arial.ttf")):
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _hp_color(frac: float):
    if frac > 0.5:
        return (60, 200, 70, 255)
    if frac > 0.2:
        return (240, 190, 40, 255)
    return (220, 60, 60, 255)


async def _fetch_sprite(session: aiohttp.ClientSession, url: str):
    if not url or not PIL_OK:
        return None
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


def _draw_hp_bar_above(draw: "ImageDraw.ImageDraw", center_x: float, sprite_top_y: float,
                        name: str, hp: int, max_hp: int, width: int = 190):
    """Draws a compact name+HP-bar plate directly above a sprite's position."""
    bar_h = 10
    plate_h = 40
    x = int(center_x - width / 2)
    x = max(6, min(x, CANVAS_W - width - 6))
    y = int(max(4, sprite_top_y - plate_h - 6))

    draw.rounded_rectangle([x, y, x + width, y + plate_h], radius=9,
                            fill=(255, 255, 255, 235), outline=(40, 40, 40, 255), width=2)
    draw.text((x + 10, y + 4), f"{name.title()}  Lv.{LEVEL}",
               font=_font(14), fill=(20, 20, 20, 255))

    bar_x, bar_y, bar_w = x + 10, y + 22, width - 20
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                            radius=5, fill=(90, 90, 90, 255))
    frac = max(hp, 0) / max_hp if max_hp else 0
    fill_w = int(bar_w * frac)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                                radius=5, fill=_hp_color(frac))


def _draw_background_flair(draw: "ImageDraw.ImageDraw", bg: dict, seed: int):
    """Season/time-of-day decorations layered on top of the sky+ground fill.
    `seed` keeps the scattered decorations (stars, snow, leaves...) stable
    across every render call for a given battle instead of jittering every
    turn, while still varying scene-to-scene."""
    rng = random.Random(seed)

    if bg["time"] == "night":
        # Moon, upper-left, plus a scatter of stars across the sky.
        mx, my, mr = int(CANVAS_W * 0.13), 46, 22
        draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(240, 240, 225, 255))
        draw.ellipse([mx - mr + 10, my - mr - 4, mx + mr + 10, my + mr - 4], fill=bg["sky"])
        for _ in range(28):
            sx = rng.randint(0, CANVAS_W)
            sy = rng.randint(0, CANVAS_H - 130)
            s = rng.choice((1, 1, 2))
            draw.ellipse([sx, sy, sx + s, sy + s], fill=(255, 255, 255, 220))
    else:
        # Sun, upper-right.
        sx, sy, sr = int(CANVAS_W * 0.88), 44, 26
        draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 235, 120, 255))

    if bg["season"] == "winter":
        for _ in range(40):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            s = rng.choice((2, 2, 3))
            draw.ellipse([fx, fy, fx + s, fy + s], fill=(255, 255, 255, 235))
    elif bg["season"] == "autumn":
        leaf_colors = [(200, 110, 40, 255), (215, 150, 40, 255), (170, 70, 30, 255)]
        for _ in range(22):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 4, fy + 4], fill=rng.choice(leaf_colors))
    elif bg["season"] == "spring":
        flower_colors = [(255, 255, 255, 255), (255, 200, 220, 255), (255, 230, 120, 255)]
        for _ in range(18):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 5, fy + 5], fill=rng.choice(flower_colors))


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon",
                               background: Optional[dict] = None) -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, with an HP bar
    rendered directly above each sprite (name, Lv.100, colour-shifting bar).
    `background` is one of the presets in BACKGROUNDS (a season/time-of-day
    combo); if omitted, one is rolled on the spot."""
    if not PIL_OK:
        return None

    bg = background or pick_background()

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg["sky"])
    draw = ImageDraw.Draw(canvas)
    _draw_background_flair(draw, bg, seed=bg.get("seed", 0))
    draw.rectangle([0, CANVAS_H - 120, CANVAS_W, CANVAS_H], fill=bg["ground"])
    draw.ellipse([CANVAS_W * 0.55 - 150, CANVAS_H - 150, CANVAS_W * 0.55 + 150, CANVAS_H - 90],
                 fill=bg["patch"])
    draw.ellipse([CANVAS_W * 0.14 - 120, CANVAS_H - 95, CANVAS_W * 0.14 + 120, CANVAS_H - 45],
                 fill=bg["patch"])

    opp_sprite = await _fetch_sprite(session, opponent_pokemon.sprite)
    player_sprite = await _fetch_sprite(session, player_pokemon.sprite)

    opp_pos = (int(CANVAS_W * 0.60), 68)
    player_pos = (int(CANVAS_W * 0.06), CANVAS_H - 232)

    opp_w = 96 * SPRITE_SCALE
    if opp_sprite:
        opp_sprite = opp_sprite.resize(
            (opp_sprite.width * SPRITE_SCALE, opp_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(opp_sprite, opp_pos)
        opp_w = opp_sprite.width

    player_w = 96 * SPRITE_SCALE
    if player_sprite:
        player_sprite = ImageOps.mirror(player_sprite)
        player_sprite = player_sprite.resize(
            (player_sprite.width * SPRITE_SCALE, player_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(player_sprite, player_pos)
        player_w = player_sprite.width

    _draw_hp_bar_above(draw, opp_pos[0] + opp_w / 2, opp_pos[1],
                        opponent_pokemon.name, opponent_pokemon.hp, opponent_pokemon.max_hp)
    _draw_hp_bar_above(draw, player_pos[0] + player_w / 2, player_pos[1],
                        player_pokemon.name, player_pokemon.hp, player_pokemon.max_hp)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="battle.png")

