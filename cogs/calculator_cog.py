"""
cogs/calculator_cog.py  —  Interactive calculator using Discord UI buttons.

Commands
────────
a!calc                 — Open an interactive button calculator
a!calc 2+2             — Instantly evaluate an expression
a!math 2+2             — Same as above (alias)
"""

import re
import discord
from discord.ext import commands

# ── Button layout ──────────────────────────────────────────────────────────────

_LAYOUT = [
    [
        ("C",  discord.ButtonStyle.danger,    "calc_clear"),
        ("CE", discord.ButtonStyle.danger,    "calc_ce"),
        ("%",  discord.ButtonStyle.secondary, "calc_pct"),
        ("÷",  discord.ButtonStyle.primary,   "calc_op_/"),
    ],
    [
        ("7",  discord.ButtonStyle.secondary, "calc_num_7"),
        ("8",  discord.ButtonStyle.secondary, "calc_num_8"),
        ("9",  discord.ButtonStyle.secondary, "calc_num_9"),
        ("×",  discord.ButtonStyle.primary,   "calc_op_*"),
    ],
    [
        ("4",  discord.ButtonStyle.secondary, "calc_num_4"),
        ("5",  discord.ButtonStyle.secondary, "calc_num_5"),
        ("6",  discord.ButtonStyle.secondary, "calc_num_6"),
        ("−",  discord.ButtonStyle.primary,   "calc_op_-"),
    ],
    [
        ("1",  discord.ButtonStyle.secondary, "calc_num_1"),
        ("2",  discord.ButtonStyle.secondary, "calc_num_2"),
        ("3",  discord.ButtonStyle.secondary, "calc_num_3"),
        ("+",  discord.ButtonStyle.primary,   "calc_op_+"),
    ],
    [
        ("±",  discord.ButtonStyle.secondary, "calc_negate"),
        ("0",  discord.ButtonStyle.secondary, "calc_num_0"),
        (".",  discord.ButtonStyle.secondary, "calc_dot"),
        ("=",  discord.ButtonStyle.success,   "calc_eq"),
    ],
]

MAX_EXPR = 40


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    if val != val:
        return "Error"
    if abs(val) > 1e15:
        return f"{val:.6e}"
    if val == int(val) and abs(val) < 1e15:
        return str(int(val))
    return f"{val:.10f}".rstrip("0").rstrip(".")


def _try_eval(expr: str) -> str:
    """Try to evaluate the expression string. Returns result or empty string on failure."""
    clean = (
        expr
        .replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
    )
    # Must only contain safe characters
    if not re.fullmatch(r"[\d\s\+\-\*\/\.\(\)\%]+", clean):
        return ""
    try:
        result = eval(clean, {"__builtins__": {}})  # noqa: S307
        return _fmt(float(result))
    except Exception:
        return ""


def _safe_eval(expr: str) -> str | None:
    """For the inline command — returns result or None."""
    result = _try_eval(expr)
    return result if result else None


# ── Calculator state ───────────────────────────────────────────────────────────

class CalcState:
    """
    Tracks a raw expression string that the user builds token by token.
    The expression stripe shows exactly what was typed.
    The result stripe shows the live evaluated value whenever possible.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.tokens: list[str] = []   # e.g. ["2", "+", "3"]
        self.finished: bool    = False # True after = is pressed

    # ── Read ───────────────────────────────────────────────────────────────────

    def expression(self) -> str:
        """Full expression string, e.g. '2 + 3'."""
        return " ".join(self.tokens) if self.tokens else "0"

    def result(self) -> str:
        """Live result. Empty string if not yet evaluable."""
        if not self.tokens:
            return "0"
        return _try_eval(self.expression()) or ""

    def _last_is_number(self) -> bool:
        return bool(self.tokens) and re.fullmatch(r"[\d.]+", self.tokens[-1]) is not None

    def _last_is_op(self) -> bool:
        return bool(self.tokens) and self.tokens[-1] in ("+", "−", "×", "÷")

    # ── Input handlers ─────────────────────────────────────────────────────────

    def input_digit(self, digit: str):
        if self.finished:
            # Start fresh but seed with the last result
            prev = self.result()
            self.reset()
            if prev and prev not in ("Error", ""):
                self.tokens = [prev]
            else:
                self.tokens = []

        if self._last_is_number():
            self.tokens[-1] += digit          # append to current number
        else:
            self.tokens.append(digit)         # new number token

    def input_dot(self):
        if self.finished:
            self.reset()
        if self._last_is_number():
            if "." not in self.tokens[-1]:
                self.tokens[-1] += "."
        else:
            self.tokens.append("0.")

    def input_operator(self, op: str):
        self.finished = False
        if not self.tokens:
            return                             # can't start with an operator
        if self._last_is_op():
            self.tokens[-1] = op              # replace the previous operator
        else:
            self.tokens.append(op)

    def input_negate(self):
        if not self._last_is_number():
            return
        n = self.tokens[-1]
        if n.startswith("-"):
            self.tokens[-1] = n[1:]
        else:
            self.tokens[-1] = "-" + n

    def input_percent(self):
        if not self._last_is_number():
            return
        try:
            val = float(self.tokens[-1]) / 100
            self.tokens[-1] = _fmt(val)
        except ValueError:
            pass

    def input_ce(self):
        """Remove only the last token (the number currently being typed)."""
        if self.finished:
            self.reset()
            return
        if self._last_is_number():
            self.tokens.pop()
        elif self._last_is_op():
            # Remove the operator too so you're back to the previous number
            self.tokens.pop()

    def input_clear(self):
        self.reset()

    def input_equals(self):
        res = self.result()
        if not res or res == "Error":
            return
        # Freeze: keep the expression visible, mark as finished
        self.finished = True


# ── Discord View ───────────────────────────────────────────────────────────────

class CalculatorView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.state    = CalcState()

        for row_idx, row in enumerate(_LAYOUT):
            for label, style, cid in row:
                btn          = discord.ui.Button(label=label, style=style, custom_id=cid, row=row_idx)
                btn.callback = self._make_callback(cid)
                self.add_item(btn)

    def _make_callback(self, cid: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "❌ This calculator belongs to someone else. Run `a!calc` to open your own.",
                    ephemeral=True,
                )
                return

            s = self.state
            if   cid == "calc_clear":         s.input_clear()
            elif cid == "calc_ce":            s.input_ce()
            elif cid == "calc_dot":           s.input_dot()
            elif cid == "calc_negate":        s.input_negate()
            elif cid == "calc_eq":            s.input_equals()
            elif cid == "calc_pct":           s.input_percent()
            elif cid.startswith("calc_num_"): s.input_digit(cid.removeprefix("calc_num_"))
            elif cid.startswith("calc_op_"):
                # Map internal op to display symbol
                raw = cid.removeprefix("calc_op_")
                sym = {"*": "×", "/": "÷", "-": "−", "+": "+"}.get(raw, raw)
                s.input_operator(sym)

            await interaction.response.edit_message(embed=self.build_embed())

        return callback

    def build_embed(self) -> discord.Embed:
        s   = self.state
        exp = s.expression()
        res = s.result()

        # Truncate long expressions from the left so the end is always visible
        if len(exp) > MAX_EXPR:
            exp = "…" + exp[-(MAX_EXPR - 1):]

        e = discord.Embed(title="🧮 Calculator", color=discord.Color.from_rgb(30, 30, 35))
        e.add_field(name="Expression", value=f"```\n{exp}\n```", inline=False)
        e.add_field(name="Result",     value=f"```\n{res or '…'}\n```", inline=False)
        e.set_footer(text="CE = delete last entry  •  C = reset  •  a!calc <expr> for quick math")
        return e

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class CalculatorCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="calc", aliases=["math", "calculator"])
    async def calc(self, ctx: commands.Context, *, expression: str = None):
        """
        Open an interactive button calculator, or evaluate an expression instantly.

        Usage:
          a!calc              — open the button calculator
          a!calc 2 + 2        — instantly print the result
          a!math (10+5)*3     — same thing
        """
        if expression:
            result = _safe_eval(expression)
            if result is None:
                await ctx.reply(
                    f"❌ Could not evaluate `{expression}`.\n"
                    "-# Use numbers and operators: `+ - * / % ( )`"
                )
            else:
                await ctx.reply(f"`{expression}` = **{result}**")
            return

        view  = CalculatorView(owner_id=ctx.author.id)
        embed = view.build_embed()
        await ctx.reply(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalculatorCog(bot))
