"""
config.py  —  Bot-wide configuration.

All custom emoji strings live here so you only need to update one file
if emojis change servers or get re-uploaded.

Usage:
    from config import E
    E.shiny          # "<:shiny:123456>"
    E.gigantamax     # "<:gigantamax:1503615787649339433>"
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Emojis:
    # ── Catch flags ───────────────────────────────────────────────────────────
    shiny:       str = "✨"
    chain_shiny: str = "🔗"
    gigantamax:  str = "<:gigantamax:1503615787649339433>"

    # ── UI / reactions ────────────────────────────────────────────────────────
    reply:       str = "<:reply:1503236369126916117>"
    caught:      str = "✅"
    fled:        str = "💨"
    spawn:       str = "🌿"
    profile:     str = "🎮"
    leaderboard: str = "🏆"
    category:    str = "📊"
    trophy_1:    str = "🥇"
    trophy_2:    str = "🥈"
    trophy_3:    str = "🥉"
    dot:         str = "•"

    def rank_emoji(self, rank: int) -> str:
        """Return a trophy emoji for top 3, otherwise the rank number."""
        return {1: self.trophy_1, 2: self.trophy_2, 3: self.trophy_3}.get(rank, f"`#{rank}`")


# Singleton — import and use everywhere
E = Emojis()
