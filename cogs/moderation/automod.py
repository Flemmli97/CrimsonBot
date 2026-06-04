import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Annotated, TypedDict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot
from utils.database import CogDatabase
from utils.sqlutils import JSON_CODEC, SQLCodec, SQLSchema, Schema


class DetectionType(Enum):
    MentionSpam = r"<@!*&*[0-9]+>"
    InviteLinks = r"(https?:\/\/)?(www\.)?((discordapp\.com/invite)|(discord\.gg))\/(\w+)"
    Regex = None


class RegexSetting(TypedDict):
    regex: str


class Setting(TypedDict):
    mute_duration: int
    mute_threshold: int


@dataclass
class AutomodRule(Schema):
    name: str
    type: Annotated[DetectionType, SQLCodec[DetectionType](lambda t: t.name, lambda s: DetectionType[s])]
    data: dict | None
    setting: Annotated[Setting, JSON_CODEC]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        name varchar(20),
                        type varchar(20),
                        data json,
                        setting json,
                    """, keys=["name"])
        return schema


@dataclass
class AutomodConfig(Schema):
    ignored_roles: list[int]

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        ignored_roles json,
                    """, keys=[])
        return schema


class SettingsBase(discord.ui.LayoutView):
    setting: discord.ui.Label

    def __init__(self) -> None:
        super().__init__()
        self.mute_duration = discord.ui.TextInput
        self.setting = discord.ui.Label(
            text="Setting",
            description="What setting you want to edit",
            component=discord.ui.Select(
                options=[
                    discord.SelectOption(label="Mute Duration", value="mute_duration"),
                    discord.SelectOption(label="Mute Threshold", value="mute_threshold"),
                    discord.SelectOption(label="Ignored Users", value="ignored_users"),
                    discord.SelectOption(label="Ignored Roles", value="ignored_roles"),
                ],
            ),
        )
        self.add_item(self.setting)

    @staticmethod
    async def send(interaction: discord.Interaction, edit: bool = False):
        send = interaction.response.edit_message if edit else interaction.response.send_message
        send(f"What setting do you want to edit?", view=SettingsBase(), ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.edit_message(f"Thanks for your feedback, {self.setting.value}!", ephemeral=True)


class Automod(commands.Cog):

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("AutoMod")
        self.config: CogDatabase[AutomodConfig] | None = None
        self.rules: CogDatabase[AutomodRule] | None = None
        self.automod_offence_tracker: dict[str, dict[int, list[datetime]]] = defaultdict(lambda: defaultdict(list))
        self.offence_updater = asyncio.create_task(self.offence_update())

    async def load_db(self) -> None:
        self.config = await self.bot.database.get_for(self, "automod", AutomodConfig)
        self.rules = await self.bot.database.get_for(self, "automod_rules", AutomodRule)

    async def cog_load(self):
        await self.load_db()

    @app_commands.command(name="automodcreate", description="Creates a new automod rule")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        type="The type of the automod rule",
        name="Name for the automod",
        data="Additional data depending on type. E.g. the regex string for regex types",
        mute_duration="Duration in mins for timeouts",
        mute_threshold="How many offences before timing out the member"
    )
    async def create(self, interaction: discord.Interaction, type: DetectionType, name: str,
                     data: Optional[str],
                     mute_duration: Optional[int], mute_threshold: Optional[int]):
        self.logger.info(f"{interaction.guild.name}: Creating automod rule type: {type} name: {name}")
        rule = await self.rules.get(interaction.guild.id, name=name)
        if rule:
            await interaction.response.send_message(f"Automod rule with name {name} already exists!")
            return
        if type == DetectionType.Regex:
            if not data:
                await interaction.response.send_message(f"Automod rule {type} requires additional data!")
                return
            data = {
                "regex": data
            }
        else:
            data = None
        rule = AutomodRule(name, type, data,
                           Setting(mute_duration=mute_duration or 60, mute_threshold=mute_threshold or 3))
        await self.rules.upsert(interaction.guild.id, rule)
        await interaction.response.send_message(f"Created new automod rule **{name}**")

    @app_commands.command(name="automodget", description="Gets the given automod rule or all")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        name="The name of the automod to delete. If not provided returns all rules"
    )
    async def get(self, interaction: discord.Interaction, name: Optional[str]):
        if not name:
            self.logger.info(f"{interaction.guild.name}: Getting automod rules")
            rules = await self.rules.get_all(interaction.guild.id)
            desc = "\n".join([f"- {rule.name} (type: `{rule.type.name}`)" for rule in rules])
            config = await self.config.get(interaction.guild.id)
            desc += f"\n\nIgnored roles:"
            for role in config.ignored_roles:
                desc += f"<@&{role}>"
            embed = discord.Embed(
                title=f"Automod rules",
                description=desc,
                color=EMBED_COLOR
            )
            await interaction.response.send_message(embed=embed)
            return
        rule = await self.rules.get(interaction.guild.id, name=name)
        if not rule:
            return
        self.logger.info(f"{interaction.guild.name}: Getting automod rule {name} {rule}")
        desc = f"Type {rule.type.name}"
        match rule.type:
            case DetectionType.Regex:
                desc += f" - Regex: {rule.data['regex']}"
        desc += f"\nMute Settings:\nDuration {rule.setting['mute_duration']}m after {rule.setting['mute_threshold']} violations"
        # Having rule based ignore settings is a pain to configure via discord...
        # So for now its global
        # desc += f"\n\nIgnored users:"
        # for user in rule.setting["ignored_users"]:
        #     desc += f"<@{user}>"
        # desc += f"\n\nIgnored roles:"
        # for user in rule.setting["ignored_roles"]:
        #     desc += f"<@&{user}>"
        embed = discord.Embed(
            title=f"Automod rule **{rule.name}**",
            description=desc,
            color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="automoddel", description="Deletes an automod rule")
    @app_commands.default_permissions(discord.Permissions(administrator=True))
    @app_commands.describe(
        name="The name of the automod to delete"
    )
    async def delete(self, interaction: discord.Interaction, name: str):
        rule = await self.rules.get(interaction.guild.id, name=name)
        if not rule:
            return
        self.logger.info(f"{interaction.guild.name}: Deleting automod rule {name}")
        await self.rules.remove(interaction.guild.id, name=name)
        await interaction.response.send_message(f"Deleted the automod rule **{name}**")

    @get.autocomplete("name")
    @delete.autocomplete("name")
    async def cmd_autocomplete(self, interaction: discord.Interaction, current: str):
        rules = await self.rules.get_all(interaction.guild.id)
        return [app_commands.Choice(name=rule.name, value=rule.name) for rule in rules if
                rule.name.startswith(current)][:25]

    @app_commands.command(name="automodignore", description="Ignores messages from given roles for automod")
    @app_commands.describe(
        role="The role to to ignore",
    )
    async def ignore_role(self, interaction: discord.Interaction, role: discord.Role):
        self.logger.info(f"{interaction.guild.name}: Adding role to ignore for automod {role.name}")
        current = await self.config.get(interaction.guild.id)
        if current and role.id not in current.ignored_roles:
            current.ignored_roles.append(role.id)
        else:
            current = AutomodConfig([role.id])
        res = await self.config.upsert(interaction.guild.id, current)
        msg = f"Ignoring messages from {role.mention} for automod rules" if res else f"Could not add role {role.mention} to ignore for automod rules"
        await interaction.response.send_message(msg)

    @app_commands.command(name="automodunignore", description="Unignores messages from given roles again for automod")
    @app_commands.describe(
        role="The role to remove",
    )
    async def unignore_user(self, interaction: discord.Interaction, role: discord.Role):
        self.logger.info(f"{interaction.guild.name}: Removing the role {role.name} to ignore for automod rules")
        current = await self.config.get(interaction.guild.id)
        if current and role.id in current.ignored_roles:
            current.ignored_roles = [c for c in current.ignored_roles if c != role.id]
            res = await self.config.upsert(interaction.guild.id, current)
        msg = f"Not ignoring messages from {role.mention} for automod rules anymore" if res else f"Could not remove role {role.mention} to ignore for automod rules"
        await interaction.response.send_message(msg)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.author.bot:
            return
        if message.author.guild_permissions.administrator:
            return
        config = await self.config.get(message.guild.id)
        if config:
            for role_config in config.ignored_roles:
                role = discord.utils.get(message.guild.roles, id=role_config)
                if role in message.author.roles:
                    return
        rules = await self.rules.get_all(message.guild.id)
        for rule in rules:
            regex: str
            match rule.type:
                case DetectionType.Regex:
                    regex: str = rule.data["regex"]
                case _:
                    regex = rule.type.value
            if re.match(regex, message.content):
                tracker = self.automod_offence_tracker[rule.name]
                tracker[message.author.id].append(datetime.now(timezone.utc))
                offence = len(tracker[message.author.id])
                embed = None
                if offence >= rule.setting["mute_threshold"]:
                    await message.author.timeout(until=timedelta(minutes=rule.setting["mute_duration"]),
                                                 reason=f"Automod {rule.name}: Message contained banned words/phrases")
                    embed = discord.Embed(
                        description=f"You have been timed out in {message.guild.name} | Automod `{rule.name}`!",
                        color=EMBED_COLOR
                    )
                elif offence == 1:
                    embed = discord.Embed(
                        description=f"{message.guild.name}: Automod `{rule.name}` triggered for your last message!",
                        color=EMBED_COLOR
                    )
                await message.delete()
                if embed:
                    await message.author.send(embed=embed)
                break

    async def offence_update(self) -> None:
        while True:
            expired = datetime.now(timezone.utc) - timedelta(minutes=30)
            for _, tracks in self.automod_offence_tracker.items():
                empty = []
                for user, times in tracks.items():
                    tracks[user] = [time for time in times if time >= expired]
                    if len(tracks[user]) == 0:
                        empty.append(user)
                for user in empty:
                    del tracks[user]
            await asyncio.sleep(60)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Automod(bot))
