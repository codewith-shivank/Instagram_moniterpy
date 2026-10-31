"""Shared Discord presentation helpers for consistent, accessible bot responses."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import discord


BRAND = "shivannnkk"

COLORS = {
    "active": discord.Color.green(),
    "monitoring": discord.Color.blurple(),
    "changed": discord.Color.gold(),
    "unavailable": discord.Color.orange(),
    "error": discord.Color.red(),
    "neutral": discord.Color.dark_grey(),
}

STATUS_LABELS = {
    "active": "ACTIVE",
    "monitoring": "MONITORING",
    "changed": "CHANGED",
    "unavailable": "UNAVAILABLE",
    "unknown": "UNKNOWN",
    "error": "ERROR",
}


def status_text(status: str) -> str:
    return STATUS_LABELS.get(status, status.upper())


def short_username(username: str) -> str:
    value = str(username or "").strip().lstrip("@")
    return value if len(value) <= 32 else value[:29] + "…"


def format_timestamp(value: datetime | str | None) -> str:
    if not value:
        return "Time unavailable"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "Time unavailable"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:f> (<t:{int(value.timestamp())}:R>)"


def base_embed(title: str, description: str | None = None, status: str = "neutral") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=COLORS.get(status, COLORS["neutral"]))
    embed.set_footer(text=f"shivannnkk • Instagram Monitor • status is based on the latest verified check")
    return embed


def error_embed(title: str, message: str) -> discord.Embed:
    return base_embed(title, message, "error")


def empty_embed(title: str, message: str) -> discord.Embed:
    return base_embed(title, message, "neutral")


def account_url(username: str) -> str:
    return f"https://www.instagram.com/{str(username).strip().lstrip('@')}/"


class PageView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def paged_embeds(title: str, rows: Iterable[str], *, page_size: int = 8, status: str = "neutral") -> list[discord.Embed]:
    values = list(rows)
    pages = []
    for offset in range(0, len(values), page_size):
        page = values[offset:offset + page_size]
        embed = base_embed(title, "\n".join(page), status)
        embed.set_footer(text=f"shivannnkk • Instagram Monitor • page {offset // page_size + 1}/{max(1, (len(values) + page_size - 1) // page_size)}")
        pages.append(embed)
    return pages or [empty_embed(title, "Nothing to show yet.")]
