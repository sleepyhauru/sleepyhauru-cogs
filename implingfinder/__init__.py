try:
    from redbot.core.utils import get_end_user_data_statement
except Exception:
    __red_end_user_data_statement__ = "This cog does not store user data."
else:
    __red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot):
    from .implingfinder import ImplingFinder

    await bot.add_cog(ImplingFinder(bot))
