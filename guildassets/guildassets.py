import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import checks, commands
from redbot.core.data_manager import cog_data_path

EMOJI_EXTENSIONS = {
    False: ".png",
    True: ".gif",
}
STICKER_EMOJI = "\N{FRAME WITH PICTURE}"
DEFAULT_STICKER_DESCRIPTION = "Imported sticker"


class GuildAssets(commands.Cog):
    """Owner-only guild emoji and sticker export/import tools."""

    __author__ = ["sleepyhauru"]
    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    @staticmethod
    def _slugify_name(name: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")
        return cleaned[:80] or fallback

    @staticmethod
    def _sanitize_emoji_name(name: str, fallback: str) -> str:
        cleaned = "".join(re.findall(r"\w+", name))
        if len(cleaned) < 2:
            cleaned = fallback
        return cleaned[:32]

    @staticmethod
    def _sanitize_sticker_name(name: str, fallback: str) -> str:
        cleaned = re.sub(r"\s+", " ", name).strip()
        if len(cleaned) < 2:
            cleaned = fallback
        return cleaned[:30]

    @staticmethod
    def _remaining_emoji_slots(guild: discord.Guild, animated: bool) -> int:
        used = sum(1 for emoji in guild.emojis if getattr(emoji, "animated", False) == animated)
        return guild.emoji_limit - used

    def _exports_root(self) -> Path:
        return cog_data_path(self) / "exports"

    def _guild_export_root(self, guild_id: int) -> Path:
        return self._exports_root() / str(guild_id)

    def _build_export_dir(self, guild: discord.Guild) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self._guild_export_root(guild.id) / timestamp

    def _latest_export_dir(self, guild_id: int) -> Optional[Path]:
        root = self._guild_export_root(guild_id)
        if not root.exists():
            return None
        candidates = [path for path in root.iterdir() if path.is_dir()]
        if not candidates:
            return None
        return sorted(candidates)[-1]

    async def _read_url(self, session: aiohttp.ClientSession, url: str) -> bytes:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _download_emoji_bytes(self, session: aiohttp.ClientSession, emoji) -> bytes:
        url = str(emoji.url)
        try:
            return await self._read_url(session, url)
        except aiohttp.ClientError:
            if getattr(emoji, "animated", False) and url.endswith(".gif"):
                return await self._read_url(session, f"{url[:-4]}.webp?animated=true")
            raise

    async def _download_sticker_bytes(self, session: aiohttp.ClientSession, sticker) -> bytes:
        if hasattr(sticker, "save"):
            try:
                from io import BytesIO

                fp = BytesIO()
                await sticker.save(fp)
                return fp.getvalue()
            except discord.DiscordException:
                pass
        return await self._read_url(session, str(sticker.url))

    async def _export_guild_assets(self, guild: discord.Guild) -> Tuple[Path, Dict[str, int]]:
        export_dir = self._build_export_dir(guild)
        emoji_dir = export_dir / "emojis"
        sticker_dir = export_dir / "stickers"
        emoji_dir.mkdir(parents=True, exist_ok=True)
        sticker_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "exported_at": export_dir.name,
            "emojis": [],
            "stickers": [],
        }
        counts = {"emojis": 0, "stickers": 0}

        async with aiohttp.ClientSession() as session:
            for index, emoji in enumerate(guild.emojis, start=1):
                filename = f"{index:03d}_{self._slugify_name(emoji.name, f'emoji_{index}')}{EMOJI_EXTENSIONS[getattr(emoji, 'animated', False)]}"
                payload = await self._download_emoji_bytes(session, emoji)
                (emoji_dir / filename).write_bytes(payload)
                manifest["emojis"].append(
                    {
                        "name": emoji.name,
                        "animated": bool(getattr(emoji, "animated", False)),
                        "filename": f"emojis/{filename}",
                    }
                )
                counts["emojis"] += 1

            for index, sticker in enumerate(guild.stickers, start=1):
                suffix = Path(getattr(sticker, "url", "")).suffix or ".png"
                filename = f"{index:03d}_{self._slugify_name(sticker.name, f'sticker_{index}')}{suffix.lower()}"
                payload = await self._download_sticker_bytes(session, sticker)
                (sticker_dir / filename).write_bytes(payload)
                manifest["stickers"].append(
                    {
                        "name": sticker.name,
                        "description": getattr(sticker, "description", None) or DEFAULT_STICKER_DESCRIPTION,
                        "emoji": getattr(sticker, "emoji", None) or STICKER_EMOJI,
                        "filename": f"stickers/{filename}",
                    }
                )
                counts["stickers"] += 1

        (export_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return export_dir, counts

    def _load_manifest(self, export_dir: Path) -> dict:
        return json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))

    async def _import_guild_assets(self, guild: discord.Guild, export_dir: Path) -> Dict[str, List[str]]:
        manifest = self._load_manifest(export_dir)
        results = {
            "added_emojis": [],
            "skipped_emojis": [],
            "added_stickers": [],
            "skipped_stickers": [],
        }

        for emoji in manifest.get("emojis", []):
            animated = bool(emoji.get("animated"))
            if self._remaining_emoji_slots(guild, animated) <= 0:
                results["skipped_emojis"].append(f"{emoji['name']} (no {'animated' if animated else 'static'} slots)")
                continue

            path = export_dir / emoji["filename"]
            name = self._sanitize_emoji_name(emoji["name"], f"emoji{len(results['added_emojis']) + 1}")
            image = path.read_bytes()
            await guild.create_custom_emoji(name=name, image=image, reason=f"Imported from guild {manifest.get('guild_id')}")
            results["added_emojis"].append(name)

        for sticker in manifest.get("stickers", []):
            if len(guild.stickers) >= guild.sticker_limit:
                results["skipped_stickers"].append(f"{sticker['name']} (no sticker slots)")
                continue

            path = export_dir / sticker["filename"]
            name = self._sanitize_sticker_name(sticker["name"], f"sticker {len(results['added_stickers']) + 1}")
            file = discord.File(path)
            await guild.create_sticker(
                name=name,
                description=sticker.get("description") or DEFAULT_STICKER_DESCRIPTION,
                emoji=sticker.get("emoji") or STICKER_EMOJI,
                file=file,
                reason=f"Imported from guild {manifest.get('guild_id')}",
            )
            results["added_stickers"].append(name)

        return results

    @commands.group(name="guildassets")
    @checks.is_owner()
    @commands.guild_only()
    async def guildassets(self, ctx: commands.Context):
        """Export or import a guild's emojis and stickers."""

    @guildassets.command(name="export")
    async def guildassets_export(self, ctx: commands.Context):
        """Download this server's current emojis and stickers into the bot's data folder."""
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command only works in a server.")
            return

        async with ctx.typing():
            export_dir, counts = await self._export_guild_assets(guild)

        await ctx.send(
            f"Exported {counts['emojis']} emojis and {counts['stickers']} stickers from `{guild.name}`.\n"
            f"Saved to `{export_dir}`\n"
            f"To import into another server later, run `[p]guildassets import {guild.id}` there."
        )

    @guildassets.command(name="import")
    @commands.bot_has_permissions(manage_emojis_and_stickers=True)
    async def guildassets_import(self, ctx: commands.Context, source_guild_id: int):
        """Import the latest export from another server into this one."""
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command only works in a server.")
            return

        export_dir = self._latest_export_dir(source_guild_id)
        if export_dir is None:
            await ctx.send(f"No export found for guild ID `{source_guild_id}`.")
            return

        async with ctx.typing():
            results = await self._import_guild_assets(guild, export_dir)

        lines = [
            f"Imported from `{source_guild_id}` into `{guild.name}` using `{export_dir.name}`.",
            f"Added emojis: {len(results['added_emojis'])}",
            f"Added stickers: {len(results['added_stickers'])}",
        ]
        if results["skipped_emojis"]:
            lines.append(f"Skipped emojis: {', '.join(results['skipped_emojis'])}")
        if results["skipped_stickers"]:
            lines.append(f"Skipped stickers: {', '.join(results['skipped_stickers'])}")
        await ctx.send("\n".join(lines))
