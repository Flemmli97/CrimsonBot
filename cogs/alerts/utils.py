from dataclasses import dataclass
from typing import TypedDict, Literal, Callable, Awaitable

from utils.bot import Bot
from utils.sqlutils import Schema, SQLSchema

type Platform = Literal["youtube", "twitch"]
type VideoTypes = Literal["videos", "live", "upcoming", "shorts"]


@dataclass
class Listener:
    name: str
    guild_id: int


class MediaInfo(TypedDict):
    source: Platform
    user: str
    user_name: str
    channel_url: str
    title: str
    url: str
    thumbnail: str
    game: str
    video_type: VideoTypes


class ProfileInfo(TypedDict):
    user_name: str
    url: str
    thumbnail: str


@dataclass
class ServerAlertSettings(Schema):
    name: str
    message: str
    alert_role: int | None
    twitch_channel: str | None
    youtube_channel: str | None
    youtube_video_types: list[VideoTypes]
    alert_channel: int

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        name varchar(64),
                        message varchar(1024),
                        alert_role int(25),
                        twitch_channel varchar(40),
                        youtube_channel varchar(40),
                        youtube_video_types json,
                        alert_channel int(25),
                    """, keys=["name"])
        return schema

    async def display(self, twitch: Callable[[str], Awaitable[ProfileInfo]],
                      youtube: Callable[[str], Awaitable[ProfileInfo]], bot: Bot) -> str:
        twitch_pfp = await twitch(self.twitch_channel) if self.twitch_channel else None
        twitch = f'Twitch [{twitch_pfp["user_name"]}]({twitch_pfp["url"]})' if twitch_pfp else ''
        youtube_pfp = await youtube(self.youtube_channel) if self.youtube_channel else None
        youtube = f'Youtube [{youtube_pfp["user_name"]}]({youtube_pfp["url"]})' if youtube_pfp else ''
        ping = f'<@&{self.alert_role}> ' if self.alert_role else ''
        return f"{ping}**{self.name}**{f' - {twitch}' if twitch else ''}{f' - {youtube}' if youtube else ''}{f' in {bot.get_channel(self.alert_channel).mention}'}"
