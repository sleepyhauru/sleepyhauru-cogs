from .kagi import Kagi

async def setup(bot):
    await bot.add_cog(Kagi(bot))