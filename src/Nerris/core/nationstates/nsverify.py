"""
This module contains all the stuff for NSVerify Functionality.
"""
import asyncio
from typing import Optional, Any

import discord
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import Session

import Nerris.exceptions
from Nerris.database import models
from Nerris.ns_api import ns
from Nerris.core.nationstates import __VERSION__

VERIFIED = "verified"
RESIDENT = "resident"


class NSVerify(commands.Cog):
    """
    NSVerify Cog
    """
    ns_client: ns.NationStatesClient
    users_verifying: dict[Any, Any] = {}

    def __init__(self, bot):
        self.nerris = bot
        self.nerris.register_meaning("verified", suppress_error=True)
        self.nerris.register_meaning("resident", suppress_error=True)

    async def cog_load(self):
        user_agent = ns.create_user_agent(self.nerris.config["CONTACT_INFO"],
                                          self.nerris.config["NATION"],
                                          self.nerris.config["REGION"])
        user_agent = "NSVerify-Cog/{} {}".format(__VERSION__, user_agent)
        self.ns_client = await ns.NationStatesClient(self.nerris.reusable_session,
                                                     user_agent=user_agent).build()

    def link_roles(self, verified_role: Optional[discord.Role], resident_role: Optional[discord.Role],
                   guild: discord.Guild, override: Optional[bool] = False) -> str:
        if verified_role is None and resident_role is None:
            raise Nerris.exceptions.NoRoles()

        guild = self.nerris.database.get_guild(guild.id)

        if verified_role is not None:
            self.nerris.add_role(verified_role, guild, VERIFIED.casefold(), override=override)

        if resident_role is not None:
            self.nerris.add_role(resident_role, guild, RESIDENT.casefold(), override=override)

        if verified_role is not None and resident_role is not None:
            return ("A Natural 20, a critical success! I've obtained the mythical +1 roles of {} and {}!"
                    ).format(verified_role.name, resident_role.name)
        if verified_role is not None:
            return ("Looks like I found the mythical role of {}...now to find the other piece."
                    ).format(verified_role)
        return ("Looks like I found the mythical role of {}...now to find the other piece."
                ).format(resident_role)

    @commands.hybrid_command()  # type: ignore
    @commands.is_owner()
    @commands.guild_only()
    async def link_region(self, ctx, region_name: str, verified_role: Optional[discord.Role],
                          resident_role: Optional[discord.Role]):
        region = await self.nerris.ns_client.get_region(region_name)
        with Session(self.nerris.database.engine) as session:
            new_region = self.nerris.database.register_region(region.name, session=session)
            new_guild = self.nerris.database.register_guild(snowflake=ctx.guild.id, session=session)
            self.nerris.database.link_guild_region(new_guild, new_region)
            session.commit()  # flush?

            try:
                self.link_roles(verified_role, resident_role, ctx.message, override=False)
                await ctx.send("The region has been registered to this server along with the roles!")

                users = session.scalars(select(models.User.snowflake)).all()
                if users:
                    members = await ctx.guild.query_members(user_ids=users)
                    for member in members:
                        await self.nerris.give_verified_roles_one_guild(member, ctx.guild)

            except Nerris.exceptions.NoRoles:
                await ctx.send("I've added that world to my maps!")
            except Nerris.exceptions.InvalidGuild as Guild:
                await ctx.send("Looks like you don't have a region associated with this server!")
            except Nerris.exceptions.InvalidRole as Role:
                await ctx.send("Oh no, I rolled a Nat 1! I can't currently add that role!")
            except Nerris.exceptions.InvalidMeaning as Meaning:
                await ctx.send("Oh no, I've lost my notes! I can't currently add roles!")
            except Nerris.exceptions.RoleOverwrite as Role:
                await ctx.send(
                "Unfortunately that would overwrite a role. Use `\\link_roles` with overwrite_roles set to True")

    @commands.hybrid_command()  # type: ignore
    @commands.is_owner()
    @commands.guild_only()
    async def unlink_region(self, ctx, region_name: str):
        ns_region = await self.nerris.ns_client.get_region(region_name.replace(" ", "_"))
        region = self.nerris.database.get_region(ns_region.name)
        guild = self.nerris.database.get_guild(ctx.guild.id)

        if region is not None and guild is not None:
            if region in guild.regions:
                self.nerris.database.unlink_guild_region(guild, region)
                return await ctx.send("I've removed this region from my maps!")
            return await ctx.send("I couldn't find that region...")
        await ctx.send("I couldn't find that region or guild...")

    @commands.hybrid_command()  # type: ignore
    @commands.guild_only()
    async def verify_nation(self, ctx, code: Optional[str], nation: str):
        """
        Verifies a nation and assigns it to a user.
        """
        nation = await self.ns_client.get_nation(nation.replace(" ", "_"))
        if self.nerris.database.get_nation(nation.name):
            await ctx.send("That nation has a character sheet already, silly!", ephemeral=True)
            return

        if code is None:
            await ctx.send("Alrighty! Please check your DMs", ephemeral=True)
            message = await ctx.message.author.send(
                ("Hi please log into your {} now. After doing so go to this link: {}\n"
                 "Copy the code from that page and paste it here.\n"
                 "**__This code does not give anyone access to your nation or any control over it. It only allows me "
                 "to verify identity__**\n"
                 "Pretty cool, huh?").format(nation, self.nerris.ns_client.get_verify_url()))
            self.users_verifying[ctx.message.author.name] = (nation, message)

            await asyncio.sleep(60)
            if ctx.message.author in self.nerris.users_verifying:
                await ctx.message.author.send((
                    "Oh, you didn't want to verify? That's fine. If you change your mind just `/verify_nation` again!"))
                del self.users_verifying[ctx.message.author.name]
        else:
            message = None
            try:
                async with ctx.typing(ephemeral=True):
                    res, ns_nation = await self.nerris.verify_nation(nation, code)
                    if res:
                        message = await ctx.send(
                            "Thanks for the character sheet! I'll go ahead and put you in my campaign binder...",
                            ephemeral=True)
                    else:
                        message = await ctx.send(
                            "That's not quite right...The code is: `{}` and the nation is: `{}`...right?".format(code,
                                                                                                                 ns_nation.name),
                            ephemeral=True)

                res = await self.nerris.register_nation(ns_nation, ctx.message)
                await message.edit(content="There we go! I'll give you roles now...")

                await self.nerris.give_verified_roles(ctx.message.author)
                await message.edit(content="Done! I've given you all roles you can have!")
            except Nerris.exceptions.InvalidCode_NSVerify:
                await ctx.send("Oh no, you didn't role high enough it seems. `{}` isn't the right code!".format(code),
                               ephemeral=True)
            except (Nerris.exceptions.NoGuilds, Nerris.exceptions.NoRoles, Nerris.exceptions.NoMeanings,
                    Nerris.exceptions.NoNation, Nerris.exceptions.NoCode_NSVerify):
                await ctx.send("There was an internal error...", ephemeral=True)
                raise

    @commands.Cog.listener('on_message')
    async def _verify_nation(self, message):
        if message.guild is None and message.author.name in self.users_verifying:
            (nation, _message) = self.users_verifying[message.author.name]
        try:
            async with message.channel.typing():
                res, nation = await self.nerris.verify_nation(nation, message.content)
            await _message.edit(content=res)

            async with message.channel.typing():
                res = await self.nerris.register_nation(nation, message)
            await _message.edit(content=res)

            async with message.channel.typing():
                del self.users_verifying[message.author.name]
                await self.nerris.give_verified_roles(message.author)
            await _message.edit(content="I've given you roles in all servers I can!")
        except Nerris.exceptions.NoCode_NSVerify:
            await _message.edit(content="You need to give me the code")
        except Nerris.exceptions.InvalidCode_NSVerify as Code:
            await _message.edit(
                content="Hmm...{} isn't right. Maybe cast scry and you'll find the right one...".format(Code.args[0]))
        except (Nerris.exceptions.NoGuilds, Nerris.exceptions.NoRoles):
            await _message.edit(content="I can't give you any roles right now. Thanks for the charactersheet though!")
        except Nerris.exceptions.NoMeanings:
            await _message.edit(content="Oh...I don't think I have a roles I can give for that...")
        except Nerris.exceptions.NoNation as Nation:
            await _message.edit(content="Oh I don't have the charactersheet for {}...".format(Nation.args[0].name))

    @commands.hybrid_command()  # type: ignore
    @commands.guild_only()
    async def unverify_nation(self, ctx, nation_name: str):
        with Session(self.nerris.database.engine) as session:
            if (user := session.scalar(select(models.User).where(models.User.snowflake == ctx.author.id))) is not None:
                ns_nation = await self.nerris.ns_client.get_nation(nation_name)
                if (nation := session.scalar(
                        select(models.Nation).where(models.Nation.name == ns_nation.name))) is not None:
                    if nation in user.nations:
                        try:
                            nation.users.remove(user)
                            user.nations.remove(nation)
                        except (ValueError, KeyError):
                            pass
                        if not nation.users:
                            session.delete(nation)
                        session.commit()
                        await self.nerris.update_verified_roles(ctx.author)
                        if not user.nations:
                            session.delete(user)
                        session.commit()
        await ctx.send("I've removed your character sheet from my campaign notes.")

    @commands.hybrid_command()  # type: ignore
    @commands.is_owner()
    @commands.guild_only()
    async def link_roles(self, ctx, verified_role: Optional[discord.Role], resident_role: Optional[discord.Role],
                         overwrite_roles: Optional[bool] = False):
        try:
            await ctx.send(self.link_roles(verified_role, resident_role, ctx.message, override=overwrite_roles))

            with Session(self.nerris.database.engine) as session:
                users = session.scalars(select(models.User.snowflake)).all()
                if users:
                    members = await ctx.guild.query_members(user_ids=users)
                    for member in members:
                        await self.nerris.give_verified_roles_one_guild(member, ctx.guild)

        except Nerris.exceptions.NoRoles:
            await ctx.send("I don't know why you're trying to add roles without giving me any...")
        except Nerris.exceptions.InvalidGuild as Guild:
            await ctx.send("Looks like you don't have a region associated with this server!")
        except Nerris.exceptions.InvalidRole as Role:
            await ctx.send("Oh no, I rolled a Nat 1! I can't currently add that role!")
        except Nerris.exceptions.InvalidMeaning as Meaning:
            await ctx.send("Oh no, I've lost my notes! I can't currently add roles!")
        except Nerris.exceptions.RoleOverwrite as Role:
            await ctx.send(
                "Unfortuantely that would overwrite a role. Use `\\link_roles` with overrwrite_roles set to True")

    @commands.hybrid_command()  # type: ignore
    @commands.is_owner()
    @commands.guild_only()
    async def unlink_roles(self, ctx, verified_role: Optional[discord.Role], resident_role: Optional[discord.Role],
                           remove_roles: Optional[bool] = True):
        unlinked_roles: list[discord.Role] = []
        if verified_role and (res := self.nerris.remove_role(verified_role)) is not None:
            unlinked_roles.append(res)
        if resident_role and (res := self.nerris.remove_role(resident_role)) is not None:
            unlinked_roles.append(res)

        if remove_roles:
            for role in unlinked_roles:
                for member in role.members:
                    await member.remove_roles(role)
        if unlinked_roles:
            await ctx.send("I've gone ahead and removed that role from my notes!")
        else:
            await ctx.send("You didn't give me any valid roles to remove from notes!...")


async def setup(bot):
    """
    setup function to make this module into an extension.
    """
    await bot.add_cog(NSVerify(bot))