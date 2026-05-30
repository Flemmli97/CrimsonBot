from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

import discord
from discord import AllowedMentions, Optional, app_commands
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase
from utils.sqlutils import SQLCodec, Schema, SQLSchema


@dataclass
class WarningEntry(Schema):
    id: int
    user: int
    reason: str
    severity: int
    by: int
    created_at: Annotated[datetime, SQLCodec[datetime](lambda d: d.isoformat(), lambda s: datetime.fromisoformat(s))]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema='''
                        id int(20) NOT NULL,
                        user int(25) NOT NULL,
                        reason varchar(60),
                        severity int(4),
                        by int(25) NOT NULL,
                        created_at varchar(32) NOT NULL,
                    ''', keys=['id'])
        return schema


class Warning(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("Moderation")
        self.warnings: CogDatabase[WarningEntry] | None = None

    async def load_db(self) -> None:
        self.warnings = await self.bot.database.get_for(self, "warnings", WarningEntry)

    async def cog_load(self):
        await self.load_db()

    @app_commands.command(name='warn', description="Warns a user")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        user="The user to warn",
        reason="The reason for the warn",
        severity="The severity of the offence. Defaults to 1"
    )
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str,
                   severity: Optional[int]):
        if user.guild_permissions.administrator:
            await interaction.response.send_message(f"Could not issue a warning to {user.mention}", ephemeral=True)
            return
        severity = max(1, severity or 1)
        self.logger.info(f'{interaction.guild.name}: Warning {user.name} for {reason} with severity {severity}')
        warning = WarningEntry(interaction.id, user.id, reason, severity, interaction.user.id,
                               datetime.now(timezone.utc))
        res = await self.warnings.upsert(interaction.guild.id, warning)
        if not res:
            await interaction.response.send_message(f"Could not issue a warning to {user.mention}", ephemeral=True)
            return
        embed = discord.Embed(
            description=f"{interaction.user.mention} warned {user.mention} for **{reason}**",
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=AllowedMentions(users=[user]))
        result = await self.warnings.get_all(interaction.guild.id, user=user.id)
        current = sum(warning.severity for warning in result)
        await self.bot.send_embed_mod_log(interaction.guild,
                                          f"{interaction.user.mention} warned {user.mention} for **{reason}** in {interaction.channel.mention}\n\nThey now have a warning severity of **{current}**",
                                          interaction.user)

    @app_commands.command(name='warnings', description="Gets all warnings for a user")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        user="The user",
    )
    async def get_warns(self, interaction: discord.Interaction, user: discord.Member):
        self.logger.info(f'{interaction.guild.name}: Fetching warns for {user.name}')
        result = await self.warnings.get_all(interaction.guild.id, user=user.id)
        if len(result) == 0:
            await interaction.response.send_message(f"{user.mention} has no warnings.")
            return
        all_warnings: dict[int, list[WarningEntry]] = defaultdict(list[WarningEntry])
        severity = 0
        for warning in result:
            all_warnings[warning.by].append(warning)
            severity += warning.severity
        all_warnings = {
            by: sorted(warning, key=lambda w: w.created_at)
            for by, warning in all_warnings.items()
        }
        desc = f"User: {user.mention} - severity of Warnings {severity}\n"
        for (by, warnings) in all_warnings.items():
            desc += f"Issuer: <@{by}>\n"
            for warning in warnings:
                desc += f"{warning.reason} - <t:{round(warning.created_at.timestamp())}:s> - Severity: {warning.severity} ({warning.id})\n"
        embed = discord.Embed(
            title=f"Warning list",
            description=desc,
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='delwarn', description="Deletes a warning from a user")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        user="The user whoses warnings should be deleted",
        id="Id of the warn",
    )
    async def delete_warning(self, interaction: discord.Interaction, user: discord.Member, id: str):
        if not id.isnumeric():
            await interaction.response.send_message(f"Invalid {id}.", ephemeral=True)
            return
        id = int(id)
        self.logger.info(f'{interaction.guild.name}: Trying to delete a warn for {user.name} with id {id}')
        current = await self.warnings.get(interaction.guild.id, user=user.id, id=id)
        if not current:
            await interaction.response.send_message(f"No such warning for {user.mention} with id {id}.", ephemeral=True)
            return
        await self.warnings.remove(interaction.guild.id, user=user.id, id=id)
        await interaction.response.send_message(f"Deleted the given warning for {user.mention}")
        await self.bot.send_embed_mod_log(interaction.guild,
                                          f"{interaction.user.mention} deleted the warning `{current.reason}`({id}) for {user.mention}",
                                          interaction.user)

    @app_commands.command(name='clearwarns', description="Clears all warnings from a user")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        user="The user to clear",
    )
    async def clear_warnings(self, interaction: discord.Interaction, user: discord.Member):
        self.logger.info(f'{interaction.guild.name}: Clearing all warns for {user.name}')
        result = await self.warnings.get_all(interaction.guild.id, user=user.id)
        if len(result) == 0:
            await interaction.response.send_message(f"{user.mention} has no warnings.")
            return
        result = await self.warnings.remove(interaction.guild.id, user=user.id)
        await interaction.response.send_message(f"Cleared all warnings for {user.mention}")
        await self.bot.send_embed_mod_log(interaction.guild,
                                          f"{interaction.user.mention} cleared all warnings {user.mention}",
                                          interaction.user)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Warning(bot))
