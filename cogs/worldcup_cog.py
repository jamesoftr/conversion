"""
cogs/worldcup_cog.py  —  FIFA World Cup 2026 tracker.

API: ESPN public API  —  NO signup, NO key, NO rate limits worth worrying about.
─────────────────────────────────────────────────────────────────────────────────
Zero setup required. Just load the cog and it works.

The ESPN API is an undocumented but stable public endpoint that powers
espn.com's own scoreboard pages. It has been reliably available for years.

Endpoints used:
  Scoreboard (today's games + live):
    https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard
  Standings:
    https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings
  Schedule (all fixtures):
    https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=YYYYMMDD
  Team schedule:
    https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/teams/{teamId}/schedule
  Team list:
    https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/teams

Commands  (prefix a!)
─────────────────────
  a!wc live                — Live scores right now
  a!wc today               — All matches today
  a!wc group <A–L>         — Group standings table
  a!wc groups              — All 12 groups, paginated ◀▶
  a!wc team <name>         — Team schedule (e.g. France, Brazil)
  a!wc next [team]         — Next upcoming match (optional team filter)
  a!wc about               — Tournament info & commands
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

# ── Constants ─────────────────────────────────────────────────────────────────

ESPN_BASE    = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
ESPN_V2_BASE = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world"
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; DiscordBot)"}

CACHE_LIVE    = 60    # seconds — for live scores
CACHE_DEFAULT = 300   # 5 min   — for fixtures, standings, team lists

GROUPS = list("ABCDEFGHIJKL")

# ── ESPN status short codes ───────────────────────────────────────────────────

# competition.status.type.name values from ESPN
STATUS_EMOJI = {
    "STATUS_SCHEDULED":    "⏳",
    "STATUS_IN_PROGRESS":  "🟢",
    "STATUS_HALFTIME":     "🟡",
    "STATUS_FINAL":        "✅",
    "STATUS_FULL_TIME":    "✅",
    "STATUS_POSTPONED":    "📅",
    "STATUS_CANCELED":     "❌",
    "STATUS_SUSPENDED":    "⏸",
    "STATUS_DELAYED":      "⏸",
    "STATUS_EXTRA_TIME":   "🟢",
    "STATUS_PENALTIES":    "🎯",
}

FLAG_MAP = {
    "Argentina": "🇦🇷", "Australia": "🇦🇺", "Austria": "🇦🇹",
    "Belgium": "🇧🇪", "Bosnia and Herzegovina": "🇧🇦",
    "Brazil": "🇧🇷", "Cabo Verde": "🇨🇻", "Canada": "🇨🇦",
    "Colombia": "🇨🇴", "Congo DR": "🇨🇩", "DR Congo": "🇨🇩",
    "Croatia": "🇭🇷", "Curaçao": "🇨🇼", "Czechia": "🇨🇿",
    "Czech Republic": "🇨🇿", "Ecuador": "🇪🇨", "Egypt": "🇪🇬",
    "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "France": "🇫🇷", "Germany": "🇩🇪",
    "Ghana": "🇬🇭", "Haiti": "🇭🇹", "Iran": "🇮🇷", "Iraq": "🇮🇶",
    "Ivory Coast": "🇨🇮", "Japan": "🇯🇵", "Jordan": "🇯🇴",
    "South Korea": "🇰🇷", "Korea Republic": "🇰🇷",
    "Mexico": "🇲🇽", "Morocco": "🇲🇦", "Netherlands": "🇳🇱",
    "New Zealand": "🇳🇿", "Norway": "🇳🇴", "Panama": "🇵🇦",
    "Paraguay": "🇵🇾", "Poland": "🇵🇱", "Portugal": "🇵🇹",
    "Qatar": "🇶🇦", "Saudi Arabia": "🇸🇦", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    "Senegal": "🇸🇳", "Serbia": "🇷🇸", "South Africa": "🇿🇦",
    "Spain": "🇪🇸", "Sweden": "🇸🇪", "Switzerland": "🇨🇭",
    "Tunisia": "🇹🇳", "Turkey": "🇹🇷", "Türkiye": "🇹🇷",
    "Ukraine": "🇺🇦", "United States": "🇺🇸", "USA": "🇺🇸",
    "Uruguay": "🇺🇾", "Uzbekistan": "🇺🇿", "Venezuela": "🇻🇪",
    "Wales": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "Algeria": "🇩🇿",
}

def _flag(name: str) -> str:
    return FLAG_MAP.get(name, "🏳")


# ── Simple TTL cache ──────────────────────────────────────────────────────────

class _Cache:
    def __init__(self):
        self._store: dict = {}

    def get(self, key: str, ttl: int):
        entry = self._store.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        return None

    def set(self, key: str, val):
        self._store[key] = (time.monotonic(), val)


_cache = _Cache()


# ── API fetch ─────────────────────────────────────────────────────────────────

async def _get(url: str, params: dict = None, ttl: int = CACHE_DEFAULT) -> dict:
    cache_key = url + str(sorted((params or {}).items()))
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.get(
            url,
            params=params or {},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ESPN API returned HTTP {resp.status}")
            data = await resp.json(content_type=None)

    _cache.set(cache_key, data)
    return data


# ── ESPN data helpers ─────────────────────────────────────────────────────────

def _competitors(competition: dict) -> tuple[dict, dict]:
    """Return (home, away) competitor dicts."""
    comps = competition.get("competitors", [])
    home = next((c for c in comps if c.get("homeAway") == "home"), comps[0] if comps else {})
    away = next((c for c in comps if c.get("homeAway") == "away"), comps[1] if len(comps) > 1 else {})
    return home, away


def _team_name(competitor: dict) -> str:
    return (
        competitor.get("team", {}).get("displayName")
        or competitor.get("team", {}).get("name")
        or "TBD"
    )


def _score(competitor: dict) -> str:
    return competitor.get("score", "–")


def _event_status(competition: dict) -> tuple[str, str]:
    """Return (status_name, display_string)."""
    status = competition.get("status", {})
    stype  = status.get("type", {})
    name   = stype.get("name", "STATUS_SCHEDULED")
    desc   = stype.get("shortDetail") or stype.get("description") or ""
    return name, desc


def _status_emoji(name: str) -> str:
    return STATUS_EMOJI.get(name, "❓")


def _kickoff_ts(event: dict) -> str:
    date_str = event.get("date", "")
    if not date_str:
        return "TBD"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:F>"
    except Exception:
        return date_str


def _kickoff_relative(event: dict) -> str:
    date_str = event.get("date", "")
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:R>"
    except Exception:
        return ""


def _is_live(competition: dict) -> bool:
    name, _ = _event_status(competition)
    return name in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME",
                    "STATUS_EXTRA_TIME", "STATUS_PENALTIES")


def _is_final(competition: dict) -> bool:
    name, _ = _event_status(competition)
    return name in ("STATUS_FINAL", "STATUS_FULL_TIME")


def _is_scheduled(competition: dict) -> bool:
    name, _ = _event_status(competition)
    return name == "STATUS_SCHEDULED"


def _match_line(event: dict) -> str:
    comp        = event.get("competitions", [{}])[0]
    home, away  = _competitors(comp)
    home_name   = _team_name(home)
    away_name   = _team_name(away)
    sname, desc = _event_status(comp)
    emoji       = _status_emoji(sname)
    kick        = _kickoff_ts(event)

    if _is_final(comp) or _is_live(comp):
        score = f"**{_score(home)} – {_score(away)}**"
    else:
        score = "vs"

    status_str = f"{emoji} {desc}" if desc else emoji

    return (
        f"{_flag(home_name)} {home_name}  {score}  {away_name} {_flag(away_name)}"
        f"  ·  {status_str}  ·  {kick}"
    )


# ── Paginator ─────────────────────────────────────────────────────────────────

class Paginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page  = 0
        self._refresh()

    def _refresh(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        self.page -= 1
        self._refresh()
        await interaction.message.edit(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        self.page += 1
        self._refresh()
        await interaction.message.edit(embed=self.pages[self.page], view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class WorldCupCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Base ──────────────────────────────────────────────────────────────────

    @commands.group(name="wc", aliases=["worldcup", "wc2026"], invoke_without_command=True)
    async def wc(self, ctx: commands.Context):
        """FIFA World Cup 2026 commands."""
        await ctx.reply(
            "⚽ **World Cup 2026 — Commands:**\n"
            "`a!wc live` — Live scores right now\n"
            "`a!wc today` — All fixtures today\n"
            "`a!wc group <A–L>` — Group standings (e.g. `a!wc group F`)\n"
            "`a!wc groups` — All 12 groups, paginated\n"
            "`a!wc team <name>` — Team schedule (e.g. `a!wc team France`)\n"
            "`a!wc next [team]` — Next upcoming match\n"
            "`a!wc about` — Tournament info",
            mention_author=False,
        )

    # ── a!wc live ─────────────────────────────────────────────────────────────

    @wc.command(name="live")
    async def wc_live(self, ctx: commands.Context):
        """Show all currently live World Cup matches."""
        async with ctx.typing():
            try:
                data   = await _get(f"{ESPN_BASE}/scoreboard", ttl=CACHE_LIVE)
                events = data.get("events", [])
                live   = [
                    e for e in events
                    if _is_live(e.get("competitions", [{}])[0])
                ]
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        if not live:
            embed = discord.Embed(
                title="⚽ World Cup 2026 — Live Scores",
                description="No matches are live right now.\nUse `a!wc today` to see today's schedule.",
                color=discord.Color.greyple(),
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        lines = [_match_line(e) for e in live]
        embed = discord.Embed(
            title="🟢 World Cup 2026 — Live Now",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Cached for ~60s  •  Powered by ESPN")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!wc today ────────────────────────────────────────────────────────────

    @wc.command(name="today")
    async def wc_today(self, ctx: commands.Context):
        """Show all World Cup matches today."""
        async with ctx.typing():
            try:
                data   = await _get(f"{ESPN_BASE}/scoreboard", ttl=CACHE_LIVE)
                events = data.get("events", [])
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

        if not events:
            embed = discord.Embed(
                title=f"📅 World Cup 2026 — {date_str}",
                description="No matches today.",
                color=discord.Color.greyple(),
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Group by league/round name ESPN provides
        by_group: dict[str, list] = {}
        for e in events:
            comp  = e.get("competitions", [{}])[0]
            notes = comp.get("notes", [])
            label = notes[0].get("headline", "") if notes else ""
            if not label:
                label = e.get("season", {}).get("slug", "Group Stage").replace("-", " ").title()
            by_group.setdefault(label, []).append(e)

        embed = discord.Embed(
            title=f"📅 World Cup 2026 — {date_str}",
            color=discord.Color.gold(),
        )
        for label, evts in by_group.items():
            lines = [_match_line(e) for e in evts]
            embed.add_field(name=label or "Matches", value="\n".join(lines), inline=False)

        embed.set_footer(text="Times shown in your local timezone via Discord  •  Powered by ESPN")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!wc group ────────────────────────────────────────────────────────────

    @wc.command(name="group")
    async def wc_group(self, ctx: commands.Context, group_letter: str):
        """
        Show standings for a World Cup group.
        Example: a!wc group B
        """
        group_letter = group_letter.upper()
        if group_letter not in GROUPS:
            await ctx.reply("❌ Choose a group from A–L.", mention_author=False)
            return

        async with ctx.typing():
            try:
                data = await _get(f"{ESPN_V2_BASE}/standings")
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        # ESPN standings: data["standings"] is a list of group objects
        # each has "name" like "Group A" and "entries" list
        standings_list = data.get("standings", [])
        target = next(
            (s for s in standings_list
             if s.get("name", "").upper().endswith(group_letter)),
            None,
        )

        if not target:
            await ctx.reply(
                f"❌ Group {group_letter} standings not available yet.",
                mention_author=False,
            )
            return

        embed = _build_group_embed(group_letter, target)
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!wc groups ───────────────────────────────────────────────────────────

    @wc.command(name="groups")
    async def wc_groups(self, ctx: commands.Context):
        """Browse all 12 group standings with ◀▶ buttons."""
        async with ctx.typing():
            try:
                data = await _get(f"{ESPN_V2_BASE}/standings")
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        standings_list = data.get("standings", [])
        if not standings_list:
            await ctx.reply("❌ No standings data available yet.", mention_author=False)
            return

        pages = []
        for s in standings_list:
            name   = s.get("name", "Group ?")
            letter = name.split()[-1].upper() if name else "?"
            embed  = _build_group_embed(letter, s)
            embed.set_footer(text=f"{name}  •  Page {len(pages)+1}/{len(standings_list)}  •  Powered by ESPN")
            pages.append(embed)

        view = Paginator(pages)
        await ctx.reply(embed=pages[0], view=view, mention_author=False)

    # ── a!wc team ─────────────────────────────────────────────────────────────

    @wc.command(name="team")
    async def wc_team(self, ctx: commands.Context, *, team_name: str):
        """
        Show all fixtures for a team.
        Example: a!wc team France
        """
        async with ctx.typing():
            try:
                # Fetch team list to find the ID
                teams_data = await _get(f"{ESPN_BASE}/teams", ttl=3600)
                teams = teams_data.get("sports", [{}])[0]\
                                  .get("leagues", [{}])[0]\
                                  .get("teams", [])

                name_lower = team_name.lower()
                matched = [
                    t["team"] for t in teams
                    if name_lower in t["team"].get("displayName", "").lower()
                    or name_lower in t["team"].get("name", "").lower()
                    or name_lower in t["team"].get("abbreviation", "").lower()
                ]

                if not matched:
                    await ctx.reply(
                        f"❌ No team found matching `{team_name}`.\n"
                        "Try the full English name (e.g. `South Korea`, `United States`, `Ivory Coast`).",
                        mention_author=False,
                    )
                    return

                team    = matched[0]
                team_id = team.get("id")
                canon   = team.get("displayName", team_name)

                sched_data = await _get(f"{ESPN_BASE}/teams/{team_id}/schedule")
                events     = sched_data.get("events", [])
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        if not events:
            await ctx.reply(f"❌ No fixtures found for **{canon}**.", mention_author=False)
            return

        lines = []
        for e in events:
            comp        = e.get("competitions", [{}])[0]
            home, away  = _competitors(comp)
            home_name   = _team_name(home)
            away_name   = _team_name(away)
            is_home     = home.get("team", {}).get("id") == team_id
            opponent    = away_name if is_home else home_name
            side        = "vs" if is_home else "@"
            sname, desc = _event_status(comp)
            emoji       = _status_emoji(sname)

            if _is_final(comp) or _is_live(comp):
                my_score  = _score(home) if is_home else _score(away)
                opp_score = _score(away) if is_home else _score(home)
                score_str = f"**{my_score} – {opp_score}**"
            else:
                score_str = ""

            status_str = f"{emoji} {desc}" if desc else emoji
            kick       = _kickoff_ts(e)
            lines.append(
                f"{_flag(opponent)} {side} **{opponent}**"
                + (f"  {score_str}" if score_str else "")
                + f"  ·  {status_str}  ·  {kick}"
            )

        embed = discord.Embed(
            title=f"{_flag(canon)} {canon} — World Cup 2026 Schedule",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Powered by ESPN")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!wc next ─────────────────────────────────────────────────────────────

    @wc.command(name="next")
    async def wc_next(self, ctx: commands.Context, *, team_name: str = None):
        """
        Show the next upcoming match, optionally filtered by team.
        Example: a!wc next
                 a!wc next Germany
        """
        async with ctx.typing():
            try:
                # Fetch the next 7 days of fixtures to find something upcoming
                upcoming_events = []
                today = datetime.now(timezone.utc)
                for offset in range(14):
                    day      = today + timedelta(days=offset)
                    date_str = day.strftime("%Y%m%d")
                    data     = await _get(
                        f"{ESPN_BASE}/scoreboard",
                        params={"dates": date_str},
                        ttl=CACHE_DEFAULT,
                    )
                    for e in data.get("events", []):
                        comp = e.get("competitions", [{}])[0]
                        if _is_scheduled(comp):
                            upcoming_events.append(e)
                    if upcoming_events:
                        break   # found at least one upcoming day, stop

                if team_name and upcoming_events:
                    name_lower = team_name.lower()
                    upcoming_events = [
                        e for e in upcoming_events
                        if any(
                            name_lower in _team_name(c).lower()
                            for c in e.get("competitions", [{}])[0].get("competitors", [])
                        )
                    ]

            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        if not upcoming_events:
            label = f"**{team_name}**" if team_name else "the World Cup"
            await ctx.reply(f"📭 No upcoming fixtures found for {label}.", mention_author=False)
            return

        e    = upcoming_events[0]
        comp = e.get("competitions", [{}])[0]
        home, away = _competitors(comp)
        home_name  = _team_name(home)
        away_name  = _team_name(away)

        notes = comp.get("notes", [])
        stage = notes[0].get("headline", "Group Stage") if notes else "Group Stage"
        venue = comp.get("venue", {}).get("fullName", "TBD")

        embed = discord.Embed(
            title=f"⏭ {_flag(home_name)} {home_name} vs {away_name} {_flag(away_name)}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Kickoff", value=f"{_kickoff_ts(e)}\n{_kickoff_relative(e)}", inline=True)
        embed.add_field(name="Stage",   value=stage,                                        inline=True)
        embed.add_field(name="Venue",   value=venue,                                        inline=True)
        embed.set_footer(text="Use a!wc live once the match starts  •  Powered by ESPN")
        await ctx.reply(embed=embed, mention_author=False)

    # ── a!wc about ────────────────────────────────────────────────────────────

    @wc.command(name="about")
    async def wc_about(self, ctx: commands.Context):
        """Tournament overview and command list."""
        embed = discord.Embed(
            title="🏆 FIFA World Cup 2026",
            description=(
                "The biggest World Cup ever — 48 teams, 104 matches, 16 stadiums "
                "across **USA 🇺🇸**, **Mexico 🇲🇽**, and **Canada 🇨🇦**."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="📅 Dates",     value="June 11 – July 19, 2026",             inline=True)
        embed.add_field(name="🏟 Venues",    value="16 stadiums · 3 countries",            inline=True)
        embed.add_field(name="👕 Teams",     value="48 teams · 12 groups (A–L)",           inline=True)
        embed.add_field(name="⚽ Matches",   value="104 total (72 group + 32 knockout)",   inline=True)
        embed.add_field(name="🥇 Defending", value="🇦🇷 Argentina",                       inline=True)
        embed.add_field(name="🏟 Final",     value="MetLife Stadium, New Jersey 🇺🇸",     inline=True)
        embed.add_field(
            name="🆕 New this year",
            value=(
                "• First 48-team World Cup\n"
                "• New Round of 32 knockout stage\n"
                "• First WC Final halftime show (Shakira, Madonna, BTS)\n"
                "• Mandatory hydration breaks each half"
            ),
            inline=False,
        )
        embed.add_field(
            name="📱 Commands",
            value=(
                "`a!wc live` `a!wc today` `a!wc group F`\n"
                "`a!wc groups` `a!wc team France`\n"
                "`a!wc next Brazil`"
            ),
            inline=False,
        )
        embed.set_footer(text="Data powered by ESPN  •  No API key required")
        await ctx.reply(embed=embed, mention_author=False)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(f"❌ Missing: `{error.param.name}`", mention_author=False)
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(f"❌ {error}", mention_author=False)
        else:
            raise error


# ── Standings embed builder ───────────────────────────────────────────────────

def _build_group_embed(letter: str, standing: dict) -> discord.Embed:
    """Build a standings table embed from an ESPN standings group object."""
    entries = standing.get("entries", [])

    # Sort by rank ESPN provides, fallback to points
    entries.sort(key=lambda e: e.get("stats", [{}])[0].get("value", 0)
                 if not e.get("stats") else 0)

    lines = [
        "```",
        f"{'#':<2} {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>4}",
        "─" * 52,
    ]

    def _stat(stats: list, name: str) -> int:
        for s in stats:
            if s.get("name") == name or s.get("shortDisplayName") == name:
                return int(s.get("value", 0) or 0)
        return 0

    team_names = []
    for i, entry in enumerate(entries, 1):
        team  = entry.get("team", {})
        name  = team.get("displayName") or team.get("name") or "?"
        stats = entry.get("stats", [])
        p     = _stat(stats, "gamesPlayed")
        w     = _stat(stats, "wins")
        d     = _stat(stats, "ties")
        lo    = _stat(stats, "losses")
        gf    = _stat(stats, "pointsFor")
        ga    = _stat(stats, "pointsAgainst")
        pts   = _stat(stats, "points")
        gd    = gf - ga
        team_names.append(f"{_flag(name)} {name}")
        lines.append(
            f"{i:<2} {name[:21]:<22} {p:>2} {w:>2} {d:>2} {lo:>2} {gf:>3} {ga:>3} {gd:>+4} {pts:>4}"
        )
    lines.append("```")

    embed = discord.Embed(
        title=f"📊 World Cup 2026 — Group {letter}",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="  ·  ".join(team_names))
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(WorldCupCog(bot))
