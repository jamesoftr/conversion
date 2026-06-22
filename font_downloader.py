"""
font_downloader.py
──────────────────
Downloads Poppins fonts from the public GitHub repo at startup.
Call `await ensure_fonts()` once before your bot starts.

Source: https://github.com/cynthiaofpower/meowthfonts/tree/main/fonts
"""

import asyncio
import sys
from pathlib import Path

import aiohttp

FONTS_DIR = Path("fonts")

FONT_FILES = [
    "Poppins-Bold.ttf",
    "Poppins-Medium.ttf",
    "Poppins-MediumItalic.ttf",
    "Poppins-Regular.ttf",
    "Poppins-SemiBold.ttf",
]

RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "cynthiaofpower/meowthfonts/main/fonts/"
)


async def _download_font(session: aiohttp.ClientSession, filename: str) -> bool:
    """Download a single font file. Returns True on success."""
    dest = FONTS_DIR / filename
    if dest.exists():
        return True          # already cached

    url = RAW_BASE + filename
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                print(f"[fonts] ⚠  Failed to download {filename} (HTTP {resp.status})",
                      file=sys.stderr)
                return False
            data = await resp.read()
        dest.write_bytes(data)
        print(f"[fonts] ✅ Downloaded {filename} ({len(data):,} bytes)")
        return True
    except Exception as exc:
        print(f"[fonts] ⚠  Error downloading {filename}: {exc}", file=sys.stderr)
        return False


async def ensure_fonts() -> None:
    """
    Download all Poppins fonts if they are not already present.
    Creates the `fonts/` directory automatically.
    Safe to call multiple times — skips already-downloaded files.
    """
    FONTS_DIR.mkdir(exist_ok=True)

    missing = [f for f in FONT_FILES if not (FONTS_DIR / f).exists()]
    if not missing:
        print("[fonts] ✅ All fonts already present.")
        return

    print(f"[fonts] Downloading {len(missing)} font(s)…")
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_download_font(session, f) for f in missing),
            return_exceptions=False,
        )

    ok  = sum(results)
    bad = len(results) - ok
    if bad:
        print(f"[fonts] ⚠  {bad} font(s) failed to download. "
              "Welcome image cards may use fallback fonts.", file=sys.stderr)
    else:
        print(f"[fonts] ✅ All fonts ready.")
