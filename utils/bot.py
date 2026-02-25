import os
from logging import Logger

import aiosqlite
from discord import Intents
from discord.ext import commands
from discord.ext.commands import Context, errors, CommandNotFound, MissingRequiredArgument

from utils.database import BotDatabase


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
