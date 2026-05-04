import asyncio
import json
import re
import shutil
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

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
EMOJI_UPLOAD_DELAY = 1.5
EMOJI_UPLOAD_MAX_RETRIES = 3
EMOJI_UPLOAD_RETRY_BASE = 3.0
IMPORT_PROGRESS_EVERY = 5
EXPORT_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


class GuildAssets(commands.Cog):
    """Owner-only guild emoji and sticker export/import tools."""

    __author__ = ["sleepyhauru"]
    __version__ = "1.3.0"

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

    @staticmethod
    def _prefix(ctx: commands.Context) -> str:
        return getattr(ctx, "clean_prefix", "[p]")

    def _guild_export_root(self, guild_id: int) -> Path:
        return self._exports_root() / str(guild_id)

    def _build_export_dir(self, guild: discord.Guild) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self._guild_export_root(guild.id) / timestamp

    def _latest_export_dir(self, guild_id: int) -> Optional[Path]:
        candidates = self._list_export_dirs(guild_id)
        if not candidates:
            return None
        return candidates[-1]

    def _list_export_dirs(self, guild_id: int) -> List[Path]:
        root = self._guild_export_root(guild_id)
        if not root.exists():
            return []
        return sorted(path for path in root.iterdir() if path.is_dir())

    def _export_counts(self) -> Tuple[int, int]:
        root = self._exports_root()
        if not root.exists():
            return 0, 0

        guild_dirs = [path for path in root.iterdir() if path.is_dir()]
        total_exports = 0
        for guild_dir in guild_dirs:
            total_exports += len([path for path in guild_dir.iterdir() if path.is_dir()])
        return len(guild_dirs), total_exports

    def _status_message(self, guild: discord.Guild, prefix: str) -> str:
        guild_exports = self._list_export_dirs(guild.id)
        tracked_guilds, total_exports = self._export_counts()
        latest = guild_exports[-1].name if guild_exports else "None"
        return (
            "GuildAssets\n"
            f"Saved exports for this guild: `{len(guild_exports)}`\n"
            f"Latest export for this guild: `{latest}`\n"
            f"Tracked source guilds: `{tracked_guilds}`\n"
            f"Total saved exports: `{total_exports}`\n"
            f"Next: run `{prefix}guildassets export`, `{prefix}guildassets list`, "
            f"`{prefix}guildassets preview <source_guild_id>`, or `{prefix}guildassets import <source_guild_id>`."
        )

    def _get_export_dir(self, guild_id: int, timestamp: Optional[str] = None) -> Optional[Path]:
        if timestamp is None:
            return self._latest_export_dir(guild_id)
        if not EXPORT_TIMESTAMP_RE.fullmatch(timestamp):
            return None

        root = self._guild_export_root(guild_id)
        candidate = root / timestamp
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
        if candidate.is_dir():
            return candidate
        return None

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

    @staticmethod
    def _hash_bytes(payload: bytes) -> str:
        return sha256(payload).hexdigest()

    async def _existing_emoji_keys(self, session: aiohttp.ClientSession, guild: discord.Guild) -> Set[Tuple[str, bool, str]]:
        keys = set()
        for emoji in guild.emojis:
            if not hasattr(emoji, "url"):
                continue
            payload = await self._download_emoji_bytes(session, emoji)
            name = self._sanitize_emoji_name(getattr(emoji, "name", ""), "emoji")
            animated = bool(getattr(emoji, "animated", False))
            keys.add((name, animated, self._hash_bytes(payload)))
        return keys

    async def _existing_sticker_keys(self, session: aiohttp.ClientSession, guild: discord.Guild) -> Set[Tuple[str, str]]:
        keys = set()
        for sticker in guild.stickers:
            if not hasattr(sticker, "url") and not hasattr(sticker, "save"):
                continue
            payload = await self._download_sticker_bytes(session, sticker)
            name = self._sanitize_sticker_name(getattr(sticker, "name", ""), "sticker")
            keys.add((name, self._hash_bytes(payload)))
        return keys

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
                        "sha256": self._hash_bytes(payload),
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
                        "sha256": self._hash_bytes(payload),
                    }
                )
                counts["stickers"] += 1

        (export_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return export_dir, counts

    def _load_manifest(self, export_dir: Path) -> dict:
        return json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))

    async def _create_emoji_with_retries(self, guild: discord.Guild, *, name: str, image: bytes, reason: str) -> None:
        attempt = 0
        while True:
            try:
                await guild.create_custom_emoji(name=name, image=image, reason=reason)
                return
            except discord.HTTPException:
                attempt += 1
                if attempt > EMOJI_UPLOAD_MAX_RETRIES:
                    raise
                await asyncio.sleep(EMOJI_UPLOAD_RETRY_BASE * attempt)

    async def _plan_guild_assets_import(self, guild: discord.Guild, export_dir: Path) -> dict:
        manifest = self._load_manifest(export_dir)
        plan = {
            "source_guild_id": manifest.get("guild_id"),
            "added_emojis": [],
            "skipped_emojis": [],
            "added_stickers": [],
            "skipped_stickers": [],
            "emoji_payloads": [],
            "sticker_payloads": [],
        }

        async with aiohttp.ClientSession() as session:
            existing_emoji_keys = await self._existing_emoji_keys(session, guild)
            existing_sticker_keys = await self._existing_sticker_keys(session, guild)
            existing_animated_emoji_names = {
                self._sanitize_emoji_name(getattr(emoji, "name", ""), "emoji")
                for emoji in guild.emojis
                if bool(getattr(emoji, "animated", False))
            }
            remaining_emoji_slots = {
                False: self._remaining_emoji_slots(guild, False),
                True: self._remaining_emoji_slots(guild, True),
            }
            remaining_sticker_slots = guild.sticker_limit - len(guild.stickers)

            for emoji in manifest.get("emojis", []):
                animated = bool(emoji.get("animated"))
                path = export_dir / emoji["filename"]
                name = self._sanitize_emoji_name(emoji["name"], f"emoji{len(plan['added_emojis']) + 1}")
                image = path.read_bytes()
                asset_hash = emoji.get("sha256") or self._hash_bytes(image)
                key = (name, animated, asset_hash)

                if key in existing_emoji_keys:
                    plan["skipped_emojis"].append(f"{name} (already exists)")
                    continue
                # Discord may expose an existing animated emoji as animated webp even when the
                # exported asset on disk is gif, which makes a byte hash comparison unreliable.
                if animated and name in existing_animated_emoji_names:
                    plan["skipped_emojis"].append(f"{name} (already exists)")
                    continue
                if remaining_emoji_slots[animated] <= 0:
                    slot_label = "animated" if animated else "static"
                    plan["skipped_emojis"].append(f"{emoji['name']} (no {slot_label} slots)")
                    continue

                existing_emoji_keys.add(key)
                if animated:
                    existing_animated_emoji_names.add(name)
                remaining_emoji_slots[animated] -= 1
                plan["added_emojis"].append(name)
                plan["emoji_payloads"].append(
                    {
                        "name": name,
                        "image": image,
                    }
                )

            for sticker in manifest.get("stickers", []):
                path = export_dir / sticker["filename"]
                name = self._sanitize_sticker_name(sticker["name"], f"sticker {len(plan['added_stickers']) + 1}")
                payload = path.read_bytes()
                asset_hash = sticker.get("sha256") or self._hash_bytes(payload)
                key = (name, asset_hash)

                if key in existing_sticker_keys:
                    plan["skipped_stickers"].append(f"{name} (already exists)")
                    continue
                if remaining_sticker_slots <= 0:
                    plan["skipped_stickers"].append(f"{sticker['name']} (no sticker slots)")
                    continue

                existing_sticker_keys.add(key)
                remaining_sticker_slots -= 1
                plan["added_stickers"].append(name)
                plan["sticker_payloads"].append(
                    {
                        "name": name,
                        "path": path,
                        "description": sticker.get("description") or DEFAULT_STICKER_DESCRIPTION,
                        "emoji": sticker.get("emoji") or STICKER_EMOJI,
                    }
                )

        return plan

    def _format_import_preview(
        self,
        guild: discord.Guild,
        source_guild_id: int,
        export_dir: Path,
        preview: dict,
        prefix: str,
    ) -> str:
        lines = [
            f"Preview import from `{source_guild_id}` into `{guild.name}` using `{export_dir.name}`.",
            f"Would add emojis: {len(preview['added_emojis'])}",
            f"Would add stickers: {len(preview['added_stickers'])}",
        ]
        if preview["added_emojis"]:
            lines.append(f"Emoji plan: {', '.join(preview['added_emojis'])}")
        if preview["added_stickers"]:
            lines.append(f"Sticker plan: {', '.join(preview['added_stickers'])}")
        if preview["skipped_emojis"]:
            lines.append(f"Skipped emojis: {', '.join(preview['skipped_emojis'])}")
        if preview["skipped_stickers"]:
            lines.append(f"Skipped stickers: {', '.join(preview['skipped_stickers'])}")
        lines.append(f"Run `{prefix}guildassets import {source_guild_id} {export_dir.name}` to apply this import.")
        return "\n".join(lines)

    async def _import_guild_assets(
        self,
        guild: discord.Guild,
        export_dir: Path,
        *,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, List[str]]:
        plan = await self._plan_guild_assets_import(guild, export_dir)
        results = {
            "added_emojis": [],
            "skipped_emojis": list(plan["skipped_emojis"]),
            "added_stickers": [],
            "skipped_stickers": list(plan["skipped_stickers"]),
        }
        imported_emoji_count = 0

        source_guild_id = plan["source_guild_id"]
        for emoji in plan["emoji_payloads"]:
            await self._create_emoji_with_retries(
                guild,
                name=emoji["name"],
                image=emoji["image"],
                reason=f"Imported from guild {source_guild_id}",
            )
            results["added_emojis"].append(emoji["name"])
            imported_emoji_count += 1
            if progress_callback is not None and imported_emoji_count % IMPORT_PROGRESS_EVERY == 0:
                await progress_callback(f"Imported {imported_emoji_count} emojis so far...")
            await asyncio.sleep(EMOJI_UPLOAD_DELAY)

        for sticker in plan["sticker_payloads"]:
            file = discord.File(sticker["path"])
            await guild.create_sticker(
                name=sticker["name"],
                description=sticker["description"],
                emoji=sticker["emoji"],
                file=file,
                reason=f"Imported from guild {source_guild_id}",
            )
            results["added_stickers"].append(sticker["name"])

        return results

    @commands.group(name="guildassets", invoke_without_command=True)
    @checks.is_owner()
    @commands.guild_only()
    async def guildassets(self, ctx: commands.Context):
        """Export or import a guild's emojis and stickers."""
        assert ctx.guild is not None
        await ctx.send(self._status_message(ctx.guild, self._prefix(ctx)))

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

    @guildassets.command(name="list")
    async def guildassets_list(self, ctx: commands.Context, source_guild_id: Optional[int] = None):
        """List saved exports for a guild, or all guilds with exports."""
        if source_guild_id is not None:
            export_dirs = self._list_export_dirs(source_guild_id)
            if not export_dirs:
                await ctx.send(f"No exports found for guild ID `{source_guild_id}`.")
                return

            lines = [f"Exports for `{source_guild_id}`:"]
            for export_dir in export_dirs:
                lines.append(f"- `{export_dir.name}`")
            await ctx.send("\n".join(lines))
            return

        root = self._exports_root()
        if not root.exists():
            await ctx.send("No exports found.")
            return

        guild_dirs = sorted(path for path in root.iterdir() if path.is_dir())
        if not guild_dirs:
            await ctx.send("No exports found.")
            return

        lines = ["Saved export history:"]
        for guild_dir in guild_dirs:
            export_count = len([path for path in guild_dir.iterdir() if path.is_dir()])
            if export_count:
                lines.append(f"- `{guild_dir.name}`: {export_count} export{'s' if export_count != 1 else ''}")
        if len(lines) == 1:
            await ctx.send("No exports found.")
            return
        await ctx.send("\n".join(lines))

    @guildassets.command(name="import")
    @commands.bot_has_permissions(manage_emojis_and_stickers=True)
    async def guildassets_import(self, ctx: commands.Context, source_guild_id: int, timestamp: Optional[str] = None):
        """Import a saved export from another server into this one."""
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command only works in a server.")
            return

        export_dir = self._get_export_dir(source_guild_id, timestamp)
        if export_dir is None:
            if timestamp is None:
                await ctx.send(f"No export found for guild ID `{source_guild_id}`.")
            else:
                await ctx.send(f"No export `{timestamp}` found for guild ID `{source_guild_id}`.")
            return

        async with ctx.typing():
            async def progress_callback(message: str) -> None:
                await ctx.send(message)

            results = await self._import_guild_assets(guild, export_dir, progress_callback=progress_callback)

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

    @guildassets.command(name="preview")
    async def guildassets_preview(self, ctx: commands.Context, source_guild_id: int, timestamp: Optional[str] = None):
        """Preview a saved import without changing this server."""
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command only works in a server.")
            return

        export_dir = self._get_export_dir(source_guild_id, timestamp)
        if export_dir is None:
            if timestamp is None:
                await ctx.send(f"No export found for guild ID `{source_guild_id}`.")
            else:
                await ctx.send(f"No export `{timestamp}` found for guild ID `{source_guild_id}`.")
            return

        async with ctx.typing():
            preview = await self._plan_guild_assets_import(guild, export_dir)

        await ctx.send(
            self._format_import_preview(
                guild,
                source_guild_id=source_guild_id,
                export_dir=export_dir,
                preview=preview,
                prefix=self._prefix(ctx),
            )
        )

    @guildassets.command(name="delete")
    async def guildassets_delete(self, ctx: commands.Context, source_guild_id: int, timestamp: str):
        """Delete one saved export by guild ID and timestamp."""
        export_dir = self._get_export_dir(source_guild_id, timestamp)
        if export_dir is None:
            await ctx.send(f"No export `{timestamp}` found for guild ID `{source_guild_id}`.")
            return

        shutil.rmtree(export_dir)
        await ctx.send(f"Deleted export `{timestamp}` for guild ID `{source_guild_id}`.")
