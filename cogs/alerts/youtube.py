import asyncio
import datetime
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime as dt, timedelta
from logging import Logger
from typing import Annotated
from xml.etree import ElementTree

import aiohttp
import requests
from aiosqlite import Cursor

from cogs.alerts.utils import Listener, MediaInfo, ProfileInfo, ServerAlertSettings, VideoTypes
from utils.database import BotDatabase, GlobalDatabase
from utils.sqlutils import Schema, SQLCodec, SQLSchema, encode_sql_obj

YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id="
YOUTUBE_CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"
CHANNEL_ID_REGEX = r"<link rel=\"canonical\" href=\"https://www.youtube.com/channel/([a-zA-Z0-9_-]+)\">"
YOUTUBE_BASE_URL = "https://www.youtube.com"
VIDEO_BASE_URL = "https://www.youtube.com/watch?v="
LIVE_DETAIL_REGEX = r"\"liveBroadcastDetails\":( ?\{(?:.+?)\})"
SHORTS_REGEX = r"<link rel=\"canonical\" href=\"https://www.youtube.com/shorts/([a-zA-Z0-9_-]+)\">"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class YoutubeMediaInfo(MediaInfo):
    id: str


@dataclass
class UploadListener(Listener):
    video_types: list[VideoTypes]


@dataclass
class YoutubeVideoCache(Schema):
    id: str
    user: str
    user_name: str
    channel_id: str
    channel_url: str
    title: str
    video_type: VideoTypes
    first_seen: Annotated[dt, SQLCodec[dt](lambda d: d.isoformat(), lambda s: dt.fromisoformat(s))]

    @staticmethod
    def of(info: YoutubeMediaInfo, channel: str, first_seen: dt):
        return YoutubeVideoCache(
            id=info["id"],
            user=info["user"],
            user_name=info["user_name"],
            channel_id=channel,
            channel_url=info["channel_url"],
            title=info["title"],
            video_type=info["video_type"],
            first_seen=first_seen
        )

    @classmethod
    def to_sql_schema(cls) -> SQLSchema:
        schema: SQLSchema = SQLSchema(schema="""
                        id varchar(64) NOT NULL,
                        user varchar(64),
                        user_name varchar(64),
                        channel_id varchar(64) NOT NULL,
                        channel_url varchar(256),
                        title varchar(256),
                        video_type varchar(16) NOT NULL,
                        first_seen varchar(32) NOT NULL,
                    """, keys=["id"])
        return schema

    def to_info(self) -> MediaInfo:
        return MediaInfo(
            source="youtube",
            user=self.user,
            user_name=self.user_name,
            channel_url=self.channel_url,
            title=self.title,
            url=f"https://www.youtube.com/watch?v={self.id}",
            thumbnail=f"https://img.youtube.com/vi/{self.id}/maxresdefault.jpg",
            game="",
            video_type=self.video_type
        )


class YoutubeListener:

    def __init__(self, logger: Logger, api_key: str):
        self.logger = logger
        self.api_key = api_key
        self.listeners: dict[str, list[UploadListener]] = {}
        self.profiles: dict[str, ProfileInfo] = {}
        self.video_cache: GlobalDatabase[YoutubeVideoCache] | None = None
        self.session = None

    async def setup(self, settings: list[tuple[int, ServerAlertSettings]], database: BotDatabase):
        self.session = aiohttp.ClientSession(headers=requests.utils.default_headers())
        self.video_cache = await database.get_global("youtube_video_cache", YoutubeVideoCache)
        self.listeners = {}
        for guild, setting in settings:
            if setting.youtube_channel:
                if setting.youtube_channel not in self.listeners:
                    self.listeners[setting.youtube_channel] = []
                self.listeners[setting.youtube_channel].append(
                    UploadListener(setting.name, guild, setting.youtube_video_types))
        self.profiles = await self._fetch_yt_profile_infos([k for k in self.listeners.keys()])

    def valid(self) -> bool:
        if self.api_key:
            return True
        return False

    async def listen(self, guild: int, setting: ServerAlertSettings):
        if setting.youtube_channel not in self.listeners:
            self.listeners[setting.youtube_channel] = []
        pfp = await self.fetch_user_profile(setting.youtube_channel)
        if not pfp:
            return
        self.listeners[setting.youtube_channel].append(UploadListener(setting.name, guild,
                                                                      setting.youtube_video_types if not setting.twitch_channel else [
                                                                          "live"]))

    def unlisten(self, guild: int, channel: str, entry_name: str):
        if channel not in self.listeners:
            return
        self.listeners[channel] = [listener for listener in self.listeners[channel] if
                                   listener.name != entry_name or listener.guild_id != guild]
        if len(self.listeners[channel]) == 0:
            del self.listeners[channel]

    async def fetch_videos(self) -> dict[int, dict[str, list[MediaInfo]]]:
        if not self.listeners:
            return {}
        videos: list[YoutubeVideoCache] = []

        seen = defaultdict(set)
        all_videos = await self.video_cache.get_all()
        for v in all_videos:
            seen[v.channel_id].add(v.id)
        # Fetch all videos from feed async first
        # Does not use api
        await asyncio.gather(*[
            self._process_channel(channel=channel, collector=videos, seen=seen[channel])
            for channel in self.listeners.keys()
        ])
        streams = [video for video in all_videos if (
                video.video_type == "upcoming" or video.video_type == "live") and video.channel_id in self.listeners]
        # Now fetch the status together
        # This reduces api calls
        video_ids = list(set([video.id for video in videos] + [stream.id for stream in streams]))
        statuses = await self._check_video_status(video_ids)
        # Schedule alert for matching videos
        guilds_to_alert: dict[int, dict[str, list[MediaInfo]]] = defaultdict(lambda: defaultdict(list))
        db_entries: list[YoutubeVideoCache] = []
        for video in videos:
            status = statuses.get(video.id)
            if not status:
                continue
            video.video_type = status
            db_entries.append(video)
            guilds = self.listeners.get(video.channel_id, [])
            if len(seen[video.channel_id]) > 0 and status != "upcoming":
                for listener in guilds:
                    if status not in listener.video_types:
                        continue
                    guilds_to_alert[listener.guild_id][listener.name].append(video.to_info())
        now = dt.now(datetime.UTC)
        for stream in streams:
            # Check upcoming live streams
            status = statuses.get(stream.id)
            if status and status != stream.video_type:
                stream.video_type = status
                stream.first_seen = now
                db_entries.append(stream)
                # Went live
                if status == "live":
                    for listener in self.listeners[stream.channel_id]:
                        if status not in listener.video_types:
                            continue
                        guilds_to_alert[listener.guild_id][listener.name].append(stream.to_info())
        await self.insert_video_data(db_entries)
        await self.prune_old_videos_cache()
        return guilds_to_alert

    async def insert_video_data(self, entries: list[YoutubeVideoCache]):
        if len(entries) > 0:
            async def insert_videos(table: str, cursor: Cursor):
                encoded = [encode_sql_obj(obj) for obj in entries]
                query = list(encoded[0].keys())
                params = [
                    tuple(obj[key] for key in query)
                    for obj in encoded
                ]
                await cursor.executemany(
                    f"REPLACE INTO {table} ({', '.join(query)}) values ({', '.join(['?' for _ in query])})",
                    params
                )
                return True

            await self.video_cache.execute(insert_videos)

    async def prune_old_videos_cache(self):
        async def prune_old_videos(table: str, cursor: Cursor):
            await cursor.execute(f"""
                DELETE FROM {table}
                WHERE id IN (
                    SELECT id
                    FROM (
                        SELECT id,
                            ROW_NUMBER() OVER (
                                PARTITION BY channel_id
                                ORDER BY first_seen DESC
                            ) AS rn
                        FROM {table}
                    )
                    WHERE rn > 50
                )
                """,
                                 )
            return True

        await self.video_cache.execute(prune_old_videos)

    async def _process_channel(self, channel: str, collector: list[YoutubeVideoCache], seen: set[str]):
        async with self.session.get(YOUTUBE_CHANNEL_RSS + channel) as response:
            if response.status == 200:
                feeds = _fetch_feed_videos(await response.text(), seen)
                for feed in reversed(feeds):
                    video: YoutubeMediaInfo = {
                        "source": "youtube",
                        "user": channel,
                        "channel_url": self.profiles[channel]["url"],
                        "video_type": None,
                        **feed
                    }
                    collector.append(YoutubeVideoCache.of(info=video, channel=channel, first_seen=video["published"]))

    async def fetch_user_profile(self, channel: str):
        if channel not in self.profiles:
            pfp = await self._fetch_yt_profile_infos([channel])
            if pfp:
                self.profiles[channel] = pfp.get(channel)
        return self.profiles.get(channel)

    async def get_user_profile_else(self, channel: str) -> ProfileInfo:
        pfp = await self.fetch_user_profile(channel)
        if not pfp:
            return {
                "user_name": channel,
                "url": f"{YOUTUBE_BASE_URL}/{channel}",
                "thumbnail": "",
            }
        return pfp

    async def _fetch_yt_profile_infos(self, channels: list[str]) -> dict[str, ProfileInfo]:
        if not channels or not self.api_key:
            return {}
        async with self.session.get(
                YOUTUBE_CHANNELS_API,
                params={
                    "part": "snippet",
                    "id": ",".join(channels),
                    "key": self.api_key
                }) as response:
            results: dict[str, ProfileInfo] = {}
            if response.status == 200:
                data = await response.json()
                items = data.get("items", [])
                for item in items:
                    channel_id = item["id"]
                    handle = item["snippet"].get("customUrl")
                    path = handle if handle else f"channel/{channel_id}"
                    results[channel_id] = {
                        "user_name": item["snippet"]["title"],
                        "url": f"{YOUTUBE_BASE_URL}/{path}",
                        "thumbnail": item["snippet"]["thumbnails"]["high"]["url"]
                    }
            return results

    async def _check_video_status(self, video_ids: list[str]) -> dict[str, VideoTypes | None]:
        if len(video_ids) == 0:
            return {}
        results: dict[str, VideoTypes | None] = {video_id: None for video_id in video_ids}
        now = dt.now(datetime.UTC)
        for i in range(0, len(video_ids), 50):
            async with self.session.get(
                    YOUTUBE_VIDEOS_API,
                    params={
                        "part": "snippet, liveStreamingDetails",
                        "id": ",".join(video_ids[i:i + 50]),
                        "key": self.api_key
                    }) as response:
                data = await response.json()
                if response.status != 200:
                    self.logger.error("YouTube API error: %s", data)
                    continue
            for video in data.get("items", []):
                video_id = video["id"]
                snippet = video.get("snippet", {})
                live_details = video.get("liveStreamingDetails", {})
                broadcast_status = snippet.get("liveBroadcastContent")
                match broadcast_status:
                    case "live":
                        results[video_id] = "live"
                    case "upcoming":
                        scheduled_start = live_details.get("scheduledStartTime")
                        if scheduled_start:
                            scheduled_time = dt.fromisoformat(scheduled_start)
                            if scheduled_time > now + timedelta(days=30):
                                self.logger.info(
                                    f"Stream {video_id} scheduled more than 30 days ""in the future. Assuming regular video.")
                                results[video_id] = "videos"
                            else:
                                results[video_id] = "upcoming"
                        else:
                            results[video_id] = "upcoming"
                    case _:
                        results[video_id] = "shorts" if await self.is_short(video_id=video_id) else "videos"
        return results

    async def is_short(self, video_id):
        async with self.session.get(f"https://www.youtube.com/shorts/{video_id}") as response:
            return response.status == 200

    async def get_and_verify_youtube_channel_id(self, channel_name: str) -> str | None:
        if not channel_name.startswith("@"):
            # First check if its a handle
            async with self.session.get(f"{YOUTUBE_BASE_URL}/@{channel_name}") as response:
                if response.status == 200:
                    return re.search(CHANNEL_ID_REGEX, await response.text()).group(1)
            channel_name = channel_name.replace("channel/", "")
            async with self.session.get(f"{YOUTUBE_BASE_URL}/channel/{channel_name}") as response:
                if response.status == 200:
                    return channel_name
            return None
        async with self.session.get(f"{YOUTUBE_BASE_URL}/{channel_name}") as response:
            if response.status == 200:
                html = await response.text()
                return re.search(CHANNEL_ID_REGEX, html).group(1)
        return None


def _fetch_feed_videos(feed_text: str, seen: set[str]) -> list[dict]:
    root = ElementTree.fromstring(feed_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    entries = root.findall("atom:entry", ns)
    if not entries:
        return []
    videos = []
    for entry in entries:
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        published = dt.fromisoformat(entry.findtext("atom:published", namespaces=ns))
        if video_id in seen:
            continue
        videos.append({
            "user_name": entry.findtext("atom:author/atom:name", namespaces=ns),
            "title": entry.findtext("atom:title", namespaces=ns),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "id": video_id,
            "published": published,
        })
    return videos
