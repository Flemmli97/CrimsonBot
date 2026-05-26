import logging
import logging.handlers
import os
import shutil
import sys
import time
from pathlib import Path

import discord
import ruamel.yaml.comments as ycm
from ruamel.yaml import YAML, CommentedMap

from utils.bot import Bot

DIR = Path(__file__).resolve().parent
CONFIG_SCHEMA = f'{DIR}/utils/config_schema.yml'

DATA_PATH = os.environ.get('DATA', './data')
CONFIG_PATH = os.environ.get('CONFIG', f'./config.yml')


def path_from(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path.resolve()
    else:
        return Path(f'{DIR}/{path}').resolve()


DATA_DIR = path_from(DATA_PATH)
CONFIG_FILE = DATA_DIR / CONFIG_PATH
LOGGER_DIR = DATA_DIR / 'logs'

try:
    os.makedirs(DATA_DIR)
except FileExistsError:
    pass

try:
    os.makedirs(LOGGER_DIR)
except FileExistsError:
    pass


class LogFormatting(logging.Formatter):
    black = "\x1b[30m"
    gray = "\x1b[38m"
    blue = "\x1b[34m"
    yellow = "\x1b[33m"
    red = "\x1b[31m"
    cyan = "\x1b[36m"

    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        level_color = self.COLORS[record.levelno]
        format_string = f"{self.reset}%(asctime)s {level_color}%(levelname)s {self.reset}{self.cyan}%(name)s{self.reset} - %(message)s"
        formatter = logging.Formatter(format_string, "%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def from_schema(result, ref):
    if isinstance(result, ycm.CommentedMap):
        if isinstance(result, ycm.CommentedBase):
            result.copy_attributes(ref)
        for key in result:
            source = result[key]
            result[key] = from_schema(source, ref[key]) if ref and key in ref else source
        return result
    if isinstance(result, ycm.CommentedBase):
        result.copy_attributes(ref)
    return ref


def collect_keys(source, path: str, keys: set[str]):
    if isinstance(source, ycm.CommentedMap):
        for key in source:
            key_path = f'{path}.{key}'
            keys.add(key_path)
            collect_keys(source[key], key_path, keys)
    return keys


yaml = YAML()
yaml.preserve_quotes = True

exists = os.path.exists(CONFIG_FILE)
if not exists:
    shutil.copyfile(CONFIG_SCHEMA, CONFIG_FILE)
with open(CONFIG_FILE, 'r') as f:
    config: CommentedMap = yaml.load(f)

# Setup Loggers
logger = logging.getLogger(config.get('name', 'Bot'))
logger.setLevel('INFO')
console_handler = logging.StreamHandler()
console_handler.setFormatter(LogFormatting())
logger.addHandler(console_handler)

file_handler = logging.handlers.TimedRotatingFileHandler(filename=f'{LOGGER_DIR}/bot.log', encoding="utf-8", when="D")
file_handler_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S")
file_handler.setFormatter(file_handler_formatter)
logger.addHandler(file_handler)

if exists:
    with open(CONFIG_SCHEMA, 'r') as f:
        schema: CommentedMap = yaml.load(f)
        currentKeys = collect_keys(config, "", set())
        schemaKeys = collect_keys(schema, "", set())
        # Config keys mismatch. Try merging
        if len(schemaKeys.difference(currentKeys)) > 0:
            from_schema(schema, config)
            logger.name = schema.get('name', logger.name)
            logger.info("Mismatching config. Updating")
            back = 0
            while os.path.exists(f'{CONFIG_FILE}.back{back if back > 0 else ""}'):
                back += 1
            with open(f'{CONFIG_FILE}.back{back if back > 0 else ""}', 'w') as f:
                yaml.dump(config, f)
            with open(CONFIG_FILE, 'w') as f:
                yaml.dump(schema, f)
            config = schema
else:
    logger.error(f"A new config has been created at {CONFIG_FILE}!")
    sys.exit(1)

logger.setLevel(config.get('logging_level', 'INFO'))
logger.info(f"Config file path: {CONFIG_FILE}")

if not config["bot_token"]:
    logger.error(f"No Bot Token defined!")
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = Bot(intents=intents, config=config, logger=logger, data_directory=DATA_DIR)
logger.info(f"===== Starting bot {bot.config['name']} =====")
bot.run(bot.config["bot_token"])
