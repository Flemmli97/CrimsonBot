import os
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from typing import Optional, Callable, Awaitable

import aiosqlite
import discord
from discord import Intents, app_commands, Interaction, TextChannel, Permissions
from discord.ext import commands
from discord.ext.commands import Context, errors, CommandNotFound, MissingRequiredArgument

from utils.database import BotDatabase, CogDatabase
from utils.sqlutils import SQLSchema, Schema

EMBED_COLOR = 0x681559


@dataclass
class BotConfig(Schema):
    log_channel: int

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema='''
                        log_channel int(25) NOT NULL,
                    ''', keys=[])
        return schema


LogChannelHandler = Callable[
    [discord.TextChannel],
    Awaitable[None]
]


class Bot(commands.Bot):
    def __init__(self, *, intents: Intents, config: dict, logger: Logger, data_directory: str):
        super().__init__(intents=intents, command_prefix=config.get('prefix', '!'))
        self.directory = os.path
        self.logger = logger
        self.database: BotDatabase | None = None
        self.config = config
        self.data_directory = data_directory
        self.__base_data_dir__ = self.get_dir_with('database')
        self.event(self.on_ready)
        self.main_config: CogDatabase[BotConfig] = None
        self._temp_banning: list[tuple[int, int]] = []

    def get_config_for(self, path: str):
        return self.config.get(path, {})

    def get_dir_with(self, path: str):
        directory = f"{self.data_directory}/{path}"
        try:
            os.makedirs(directory)
        except FileExistsError:
            pass
        return directory

    async def load_db(self) -> None:
        db_path = f"{self.__base_data_dir__}/database.db"
        self.logger.info("Loading Bot Database at %s", db_path)
        self.database = BotDatabase(logger=self.logger,
                                    connection=await aiosqlite.connect(db_path))
        self.main_config = await self.database.get_for("Bot", "config", BotConfig)

    async def load_cogs(self) -> None:
        self.logger.info("Loading Cogs")
        for path, _, files in os.walk('./cogs'):
            for file_name in files:
                if file_name.endswith('.py'):
                    extension = f'{path.replace("./cogs", "cogs").replace("/", ".")}.{file_name[:-3]}'
                    self.logger.info("Loading extension: %s", extension)
                    await self.load_extension(extension)

    async def on_ready(self):
        await self.tree.sync()

    async def setup_hook(self):
        self.logger.info("Initializing Bot")

        await self.load_db()
        await self.add_cog(MainCog(self, self.logger, self.main_config))
        await self.load_cogs()
        await self.tree.sync()

    async def close(self):
        self.logger.info("Closing Bot")
        await self.database.close()

    async def on_command_error(self, ctx: Context, exception: errors.CommandError) -> None:
        if isinstance(exception, CommandNotFound):
            return None
        if isinstance(exception, MissingRequiredArgument):
            await ctx.reply(
                f"Missing arguments for command. {{{exception.param.displayed_name or exception.param.name}}} is required!",
                ephemeral=True,
                delete_after=15)
            return None
        return await super().on_command_error(ctx, exception)

    async def get_log_channel(self, guild: discord.Guild):
        config = await self.main_config.get(guild.id)
        if not config:
            return None
        channel = guild.get_channel(config.log_channel)
        if isinstance(channel, TextChannel):
            return channel
        return None

    async def send_embed_mod_log(self, guild: discord.Guild, msg: str, user: discord.Member):
        embed = discord.Embed(
            description=msg,
            timestamp=datetime.now(),
            color=EMBED_COLOR
        ).set_author(name=user.display_name, url=None, icon_url=user.display_avatar)
        await self.send_mod_log(guild, lambda ch: ch.send(embed=embed))

    async def send_mod_log(self, guild: discord.Guild, handler: LogChannelHandler):
        channel = await self.get_log_channel(guild)
        if channel:
            await handler(channel)


class MainCog(commands.Cog):
    def __init__(self, bot: Bot, logger: Logger, main_config: CogDatabase[BotConfig]):
        self.bot = bot
        self.logger = logger
        self.main_config = main_config

    @app_commands.command(name="log_channel", description="Sets the channel for bot logs")
    @app_commands.default_permissions(Permissions(administrator=True))
    @app_commands.describe(
        channel="The log channel",
    )
    async def set_log_channel(self, interaction: Interaction, channel: Optional[TextChannel]):
        self.logger.info(
            f'Setting bots log channel to {channel.name if channel else "none"} for guild {interaction.guild.name}')
        config = await self.main_config.get(interaction.guild.id)
        if not config:
            config = BotConfig(0)
        config.log_channel = channel.id if channel else 0
        await self.main_config.upsert(interaction.guild.id, config)
        await interaction.response.send_message(
            f'Log channel set to {channel.mention}' if channel else f'Removed log channel')
