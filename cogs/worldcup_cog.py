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
  a!wc today                — All matches today
  a!wc group <A–L>          — Group standings table
  a!wc groups                — All 12 groups, paginated ◀▶
  a!wc team <name>          — Team schedule (e.g. France, Brazil)
  a!wc next [team]           — Next upcoming match (optional team filter)
  a!wc about                 — Tournament info & commands

─────────────────────────────────────────────────────────────────────────────────
FIXES APPLIED (2026-06-22):

  1. _score() now handles ESPN's dict-shaped score objects, e.g.:
       {'$ref': 'http://sports.core.api.espn.pvt/...', 'value': 3.0,
        'displayValue': '3', 'winner': True, 'source': {...}}
     Previously this dict was stringified raw into Discord messages
     (visible in `a!wc team`, `a!wc live`, `a!wc today`). Now pulls
     displayValue / value out of it.

  2. Added _extract_groups() to read ESPN's *actual* confirmed standings
     shape. The standings endpoint does NOT have a flat top-level
     "standings" list — it nests groups under "children", with each
     child's entries at children[i]["standings"]["entries"]:

       {
         "children": [
           {
             "name": "Group A",
             "standings": { "entries": [ {...team entries...} ] }
           },
           ...
         ]
       }

     wc_group / wc_groups previously did `data.get("standings", [])` at
     the top level, which is always empty — that's why both commands
     reported "standings not available." Confirmed via a live dump of
     the endpoint on 2026-06-22.
"""

from __future__ import annotations

import asyncio
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

LIVE_REFRESH_INTERVAL = 60    # seconds between auto-edits of a!wc live
LIVE_MAX_IDLE_CHECKS  = 10    # stop auto-updating after this many consecutive
                               # checks with zero live matches (~10 min idle),
                               # so the task doesn't run forever in a quiet channel

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
    """
    Return a display-ready score string.

    FIX: ESPN sometimes returns "score" as a dict instead of a plain string,
    e.g. {'$ref': 'http://sports.core.api.espn.pvt/...', 'value': 3.0,
          'displayValue': '3', 'winner': True, 'source': {...}}
    Previously that raw dict got stringified directly into Discord embeds.
    Now we pull out displayValue (preferred) or value.
    """
    score = competitor.get("score", "–")
    if isinstance(score, dict):
        return score.get("displayValue") or str(score.get("value", "–"))
    return str(score)


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


# ── Standings normalization ───────────────────────────────────────────────────

def _extract_groups(data: dict) -> list[dict]:
    """
    Normalize an ESPN standings payload into a flat list of
    {"name": str, "entries": [...]} dicts.

    CONFIRMED SHAPE (live dump, 2026-06-22): ESPN's fifa.world standings
    endpoint nests groups under "children", NOT a flat top-level
    "standings" list:

        data["children"][i]["name"]                       -> "Group A"
        data["children"][i]["standings"]["entries"]        -> [ {team entries} ]

    A flat top-level "standings" list is kept as a fallback in case ESPN
    changes shape again or another endpoint is reused with this helper.
    """
    groups: list[dict] = []

    # Confirmed shape: nested under "children"
    for child in data.get("children", []):
        name = child.get("name") or child.get("abbreviation") or "Group ?"
        standings_obj = child.get("standings", {})
        entries = standings_obj.get("entries", []) if isinstance(standings_obj, dict) else []
        if entries:
            groups.append({"name": name, "entries": entries})

    if groups:
        return groups

    # Fallback shape: flat top-level "standings" list, each item a group
    flat = data.get("standings")
    if isinstance(flat, list) and flat:
        for s in flat:
            entries = s.get("entries")
            if not entries and isinstance(s.get("standings"), dict):
                entries = s["standings"].get("entries", [])
            if entries:
                groups.append({"name": s.get("name", "Group ?"), "entries": entries})

    return groups


def _build_live_embed(live_events: list[dict], *, stopped: bool = False) -> discord.Embed:
    """
    Build the 'live scores' embed. Shared by the initial a!wc live send and
    the background auto-refresh loop so both stay visually identical.
    """
    if not live_events:
        embed = discord.Embed(
            title="⚽ World Cup 2026 — Live Scores",
            description="No matches are live right now.\nUse `a!wc today` to see today's schedule.",
            color=discord.Color.greyple(),
        )
        if stopped:
            embed.set_footer(text="Auto-updates stopped (no live matches for a while)  •  Powered by ESPN")
        else:
            embed.set_footer(text=f"Auto-updating every {LIVE_REFRESH_INTERVAL}s  •  Powered by ESPN")
        return embed

    lines = [_match_line(e) for e in live_events]
    embed = discord.Embed(
        title="🟢 World Cup 2026 — Live Now",
        description="\n".join(lines),
        color=discord.Color.green(),
    )
    if stopped:
        embed.set_footer(text="Auto-updates stopped  •  Run a!wc live again to resume  •  Powered by ESPN")
    else:
        embed.set_footer(text=f"Auto-updating every {LIVE_REFRESH_INTERVAL}s  •  Powered by ESPN")
    return embed


async def _fetch_live_events() -> list[dict]:
    """Fetch the scoreboard and return only the currently-live events."""
    data   = await _get(f"{ESPN_BASE}/scoreboard", ttl=CACHE_LIVE)
    events = data.get("events", [])
    return [e for e in events if _is_live(e.get("competitions", [{}])[0])]


# ── Paginator ─────────────────────────────────────────────────────────────────

class Paginator(discord.ui.View):
    """
    NOTE: do NOT name a method on this class "_refresh". discord.py's own
    View base class defines an internal _refresh(self, components) that
    the gateway calls automatically on MESSAGE_UPDATE events to resync
    button state. Overriding it with a no-arg version causes:
        TypeError: Paginator._refresh() takes 1 positional argument but 2 were given
    Use a differently-named helper instead (here: _update_button_state).
    """
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page  = 0
        self._update_button_state()

    def _update_button_state(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        self.page -= 1
        self._update_button_state()
        await interaction.message.edit(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        self.page += 1
        self._update_button_state()
        await interaction.message.edit(embed=self.pages[self.page], view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class WorldCupCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One auto-updating "a!wc live" task per channel. Keyed by channel
        # ID. Starting a new a!wc live in the same channel cancels whatever
        # task is already running here before starting the new one, so only
        # the most recent message ever keeps updating.
        self._live_tasks: dict[int, asyncio.Task] = {}

    def cog_unload(self):
        # Make sure no background loops keep running (and keep hitting the
        # ESPN API) after the cog is unloaded/reloaded.
        for task in self._live_tasks.values():
            task.cancel()
        self._live_tasks.clear()

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
        """
        Show live World Cup matches, auto-updating every ~60s.
        Only one auto-updating message per channel — running this again
        stops the previous one and starts updating the new message instead.
        """
        # Stop any previous auto-updater running in this channel so we never
        # have two messages fighting to be "the live one."
        old_task = self._live_tasks.get(ctx.channel.id)
        if old_task and not old_task.done():
            old_task.cancel()

        async with ctx.typing():
            try:
                live = await _fetch_live_events()
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        embed = _build_live_embed(live)
        message = await ctx.reply(embed=embed, mention_author=False)

        task = asyncio.create_task(self._live_update_loop(message, ctx.channel.id))
        self._live_tasks[ctx.channel.id] = task

    async def _live_update_loop(self, message: discord.Message, channel_id: int):
        """
        Background loop that edits `message` in place every
        LIVE_REFRESH_INTERVAL seconds with the latest live scores.

        Stops when:
          - it's cancelled (a newer a!wc live started in the same channel,
            or the cog is unloaded)
          - the message was deleted (discord.NotFound)
          - the bot lost permission to edit it (discord.Forbidden)
          - there have been no live matches for LIVE_MAX_IDLE_CHECKS checks
            in a row, so a quiet channel doesn't poll ESPN forever
        """
        idle_checks = 0
        try:
            while True:
                await asyncio.sleep(LIVE_REFRESH_INTERVAL)

                try:
                    live = await _fetch_live_events()
                except Exception:
                    # Transient ESPN hiccup — skip this tick, try again next time.
                    continue

                idle_checks = idle_checks + 1 if not live else 0
                stopped = idle_checks >= LIVE_MAX_IDLE_CHECKS

                embed = _build_live_embed(live, stopped=stopped)
                try:
                    await message.edit(embed=embed)
                except discord.NotFound:
                    return   # message deleted — nothing left to update
                except discord.Forbidden:
                    return   # lost perms — give up quietly

                if stopped:
                    return
        except asyncio.CancelledError:
            # A newer a!wc live took over, or the cog unloaded. Don't touch
            # the message here — the new task (or nothing) owns it now.
            raise
        finally:
            # Only clear ourselves out of the registry if we're still the
            # one registered for this channel (a newer task may already
            # have replaced us by the time we get here).
            if self._live_tasks.get(channel_id) is asyncio.current_task():
                del self._live_tasks[channel_id]

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

        standings_list = _extract_groups(data)
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
    async def wc_groups(self, ctx: commands.Context, groups_per_page: int = 4):
        """
        Browse all 12 group standings with ◀▶ buttons, several groups per page.
        Example: a!wc groups          (4 groups per page, 3 pages total)
                 a!wc groups 6         (6 groups per page, 2 pages total)
        """
        groups_per_page = max(1, min(groups_per_page, 12))

        async with ctx.typing():
            try:
                data = await _get(f"{ESPN_V2_BASE}/standings")
            except Exception as exc:
                await ctx.reply(f"❌ ESPN API error: {exc}", mention_author=False)
                return

        standings_list = _extract_groups(data)
        if not standings_list:
            await ctx.reply("❌ No standings data available yet.", mention_author=False)
            return

        # Keep groups in A–L order rather than whatever order ESPN returns
        standings_list.sort(key=lambda s: s.get("name", "Group ZZ"))

        chunks = [
            standings_list[i : i + groups_per_page]
            for i in range(0, len(standings_list), groups_per_page)
        ]
        total_pages = len(chunks)

        pages = [
            _build_groups_page_embed(chunk, page_num=i + 1, total_pages=total_pages)
            for i, chunk in enumerate(chunks)
        ]

        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
            return

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


# ── Standings table helpers ───────────────────────────────────────────────────

def _stat(stats: list, name: str) -> int:
    for s in stats:
        if s.get("name") == name or s.get("shortDisplayName") == name:
            return int(s.get("value", 0) or 0)
    return 0


def _sorted_entries(standing: dict) -> list[dict]:
    """Sort group entries by ESPN's note.rank, falling back to points."""
    entries = standing.get("entries", [])

    def _sort_key(entry: dict):
        note = entry.get("note") or {}
        rank = note.get("rank")
        if rank is not None:
            return rank
        stats = entry.get("stats", [])
        for s in stats:
            if s.get("name") == "points":
                return -float(s.get("value", 0) or 0)
        return 0

    return sorted(entries, key=_sort_key)


def _group_table_block(standing: dict, name_width: int = 22) -> str:
    """
    Build just the monospace standings table (no embed, no surrounding
    text) for one group, e.g.:

        #  Team                    P  W  D  L  GF  GA   GD  Pts
        ─────────────────────────────────────────────────
        1  Mexico                  2  2  0  0   5   1   +4    6
        ...

    Shared by both the single-group view (a!wc group <letter>) and the
    multi-group-per-page view (a!wc groups), so the column layout only
    needs to be defined in one place.

    NOTE: deliberately plain text, no flag emoji. Flag glyphs (especially
    regional-indicator pairs like England/Scotland/Wales) render at
    inconsistent widths across clients/fonts, which throws off monospace
    column alignment — rows drift depending on which flags appear. Flags
    are still used elsewhere (titles, footers) where alignment doesn't matter.
    """
    entries = _sorted_entries(standing)

    lines = [
        f"{'#':<2} {'Team':<{name_width}} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>4}",
        "─" * (12 + name_width + 18),
    ]
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
        label = name[:name_width]
        lines.append(
            f"{i:<2} {label:<{name_width}} {p:>2} {w:>2} {d:>2} {lo:>2} {gf:>3} {ga:>3} {gd:>+4} {pts:>4}"
        )
    return "\n".join(lines)


def _build_group_embed(letter: str, standing: dict) -> discord.Embed:
    """Build a single-group standings embed (used by a!wc group <letter>)."""
    table = _group_table_block(standing)
    team_names = [
        f"{_flag(e.get('team', {}).get('displayName') or e.get('team', {}).get('name') or '?')} "
        f"{e.get('team', {}).get('displayName') or e.get('team', {}).get('name') or '?'}"
        for e in _sorted_entries(standing)
    ]

    embed = discord.Embed(
        title=f"📊 World Cup 2026 — Group {letter}",
        description=f"```\n{table}\n```",
        color=discord.Color.blue(),
    )
    embed.set_footer(text="  ·  ".join(team_names))
    return embed


def _build_groups_page_embed(
    groups: list[dict], page_num: int, total_pages: int
) -> discord.Embed:
    """
    Build one embed containing MULTIPLE groups' standings tables — used by
    a!wc groups so the whole tournament can be browsed in a few pages
    instead of one button click per group.
    """
    embed = discord.Embed(
        title="📊 World Cup 2026 — Group Standings",
        color=discord.Color.blue(),
    )
    for s in groups:
        name  = s.get("name", "Group ?")
        # "Group A" -> "A"
        letter = name.split()[-1].upper() if name else "?"
        table  = _group_table_block(s, name_width=18)
        embed.add_field(
            name=f"Group {letter}",
            value=f"```\n{table}\n```",
            inline=False,
        )
    embed.set_footer(text=f"Page {page_num}/{total_pages}  •  Powered by ESPN")
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(WorldCupCog(bot))
