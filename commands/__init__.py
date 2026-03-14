from .commands import commands


async def setup(bot):
    await bot.add_cog(commands(bot))