from .linkedin import LinkedIn


async def setup(bot):
    await bot.add_cog(LinkedIn(bot))