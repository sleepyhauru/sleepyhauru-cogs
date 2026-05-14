async def setup(bot):
    from .ollamachat import OllamaChat

    await bot.add_cog(OllamaChat(bot))
