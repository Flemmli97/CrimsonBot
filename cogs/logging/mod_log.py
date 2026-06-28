from datetime import datetime
from io import BytesIO

import discord
from discord.ext import commands

from utils.bot import EMBED_COLOR, Bot


class ModLog(commands.Cog):
    """
    Note: message logging only works for new messages with the bot
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.logger = self.bot.logger.getChild("Moderation")

    @staticmethod
    def message_link(message: discord.Message):
        return f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot:
            return
        desc = f"**Message by {before.author.mention} edited: {ModLog.message_link(before)}**"
        desc += f"\n== **Before** ==\n{before.content}"
        desc += f"\n== **After** ==\n{after.content}"
        await self.bot.send_embed_mod_log(before.guild,
                                          desc,
                                          before.author)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return
        desc = f"**Message by {message.author.mention} deleted in {message.channel.mention}**"
        embeds = []
        if len(message.content) > 0:
            main_msg = discord.Embed(
                description=f"{desc}\n{message.content}",
                timestamp=datetime.now(),
                color=EMBED_COLOR
            ).set_author(name=message.author.display_name, url=None, icon_url=message.author.display_avatar)
            embeds.append(main_msg)
        images = [att for att in message.attachments if att.content_type and att.content_type.startswith("image/")]
        files = []
        img_num = len(images)
        if img_num > 0:
            if len(embeds) == 0:
                img_desc = desc.replace("Message", f"Images" if img_num > 1 else f"Image")
            else:
                img_desc = f"**{img_num} images deleted**" if img_num > 1 else f"**Image deleted**"
            try:
                files = [discord.File(
                    BytesIO(await images[0].read(use_cached=True)),
                    filename=images[0].filename
                )]
                images_msg = discord.Embed(
                    description=img_desc,
                    timestamp=datetime.now(),
                    color=EMBED_COLOR
                ).set_author(name=message.author.display_name, url=None,
                             icon_url=message.author.display_avatar).set_image(
                    url=f"attachment://{images[0].filename}")
                embeds.append(images_msg)
            except discord.NotFound:
                img_desc += "\nUnable to cache sent image"
                images_msg = discord.Embed(
                    description=img_desc,
                    timestamp=datetime.now(),
                    color=EMBED_COLOR
                ).set_author(name=message.author.display_name, url=None, icon_url=message.author.display_avatar)
                embeds.append(images_msg)
        others = [att for att in message.attachments if att not in images]
        att_num = len(others)
        if att_num > 0:
            if len(embeds) == 0:
                att_desc = desc.replace("Message", f"Attachments" if att_num > 1 else f"Attachment")
            else:
                att_desc = f"**{img_num} attachments deleted**" if att_num > 1 else f"**Attachment deleted**"
            att_desc += "\n "
            att_desc += "\n".join([f'`{att.filename}`' for att in others])
            attachments_msg = discord.Embed(
                description=att_desc,
                timestamp=datetime.now(),
                color=EMBED_COLOR
            ).set_author(name=message.author.display_name, url=None, icon_url=message.author.display_avatar)
            embeds.append(attachments_msg)
        await self.bot.send_mod_log(message.guild, lambda ch: ch.send(embeds=embeds, files=files))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        non_bots = [message for message in messages if not message.author.bot]
        if len(non_bots) > 5:
            channels = set()
            for message in non_bots:
                channels.add(message.channel.mention)
            desc = f"**{len(non_bots)} Messages deleted in {', '.join(channels)}**"
            await self.bot.send_embed_mod_log(non_bots[0].guild,
                                              desc,
                                              message.author)
            return
        for message in non_bots:
            await self.on_message_delete(message)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not before.is_timed_out() and after.is_timed_out():
            await self.bot.send_embed_mod_log(before.guild,
                                              f"**{before.mention} got timed out**",
                                              before)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        match entry.action:
            case discord.AuditLogAction.kick:
                source = f" by {entry.user.mention}" if entry.user else ""
                reason = f" because {entry.reason}" if entry.reason else ""
                embed = discord.Embed(
                    description=f"**<@{entry.target.id}> got kicked {source} {reason}**",
                    timestamp=datetime.now(),
                    color=EMBED_COLOR
                )
                await self.bot.send_mod_log(entry.guild, lambda ch: ch.send(embed=embed))
            case discord.AuditLogAction.ban:
                # Checking if user was temp or perma banned
                current = len(self.bot._temp_banning)
                self.bot._temp_banning = [(guild_id, user_id) for (guild_id, user_id) in self.bot._temp_banning if
                                          guild_id != entry.guild.id and user_id != entry.target.id]
                # If not changed this means user was not temp banned
                if current == len(self.bot._temp_banning):
                    source = f" by {entry.user.mention}" if entry.user else ""
                    reason = f" because {entry.reason}" if entry.reason else ""
                    embed = discord.Embed(
                        description=f"**<@{entry.target.id}> got banned {source} {reason}**",
                        timestamp=datetime.now(),
                        color=EMBED_COLOR
                    )
                    await self.bot.send_mod_log(entry.guild, lambda ch: ch.send(embed=embed))
            case discord.AuditLogAction.unban:
                source = f" by {entry.user.mention}" if entry.user else ""
                embed = discord.Embed(
                    description=f"**<@{entry.target.id}> got unbanned {source}**",
                    timestamp=datetime.now(),
                    color=EMBED_COLOR
                )
                await self.bot.send_mod_log(entry.guild, lambda ch: ch.send(embed=embed))


async def setup(bot: Bot) -> None:
    await bot.add_cog(ModLog(bot))
