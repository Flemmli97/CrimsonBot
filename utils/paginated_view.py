from math import ceil
from typing import Awaitable, Callable, TypeVar

import discord

T = TypeVar("T")


class PaginationView(discord.ui.View):
    def __init__(self, sender: int,
                 entries: list[T],
                 generator: Callable[[list[T]], Awaitable[discord.Embed]],
                 page=0, amount=10):
        super().__init__(timeout=180)
        self.sender = sender
        self.entries = entries
        self.generator = generator
        self.page = page
        self.amount = amount
        self.max_pages = ceil(len(entries) / amount) - 1
        self.previous_button = discord.utils.get(self.children, label="Previous")
        self.next_button = discord.utils.get(self.children, label="Next")
        self.update_buttons()

    def update_buttons(self):
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_pages

    async def get_embed(self):
        paginated = self.entries[self.page:self.page + self.amount]
        return await self.generator(paginated)

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        emoji="◀️"
    )
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.sender:
            await interaction.response.defer()
            return
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.primary,
        emoji="▶️"
    )
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.sender:
            await interaction.response.defer()
            return
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=await self.get_embed(), view=self)
