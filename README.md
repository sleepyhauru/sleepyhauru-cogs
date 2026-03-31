# sleepyhauru-cogs

This is an addon repo for [Red Discord Bot](https://github.com/Cog-Creators/Red-DiscordBot), built around the cogs I use on my own servers.

These cogs target Red 3.5-era installs and use modern Discord interaction features where it makes sense.

## Installation

To add one of these cogs to your instance of Red, run the following commands one by one (`[p]` is your prefix):

```text
[p]load downloader
[p]repo add sleepyhauru-cogs https://github.com/sleepyhauru/sleepyhauru-cogs
[p]cog install sleepyhauru-cogs [cog name]
[p]load [cog name]
```

You may be prompted to respond with `I agree` during install.

## Included Cogs

### AddImage (`addimage`)

Save images for the bot to upload later, similar to aliases but for attachments. Supports guild-specific images, owner-managed global images, renaming, deletion, and per-guild size limits.

### Commands (`commands`)

Provides a single embedded command browser for selected installed cogs. Includes owner configuration for allow/deny lists and auto-discovery behavior.

![Commands example](docs/screenshots/commands.png)

### Deepfry (`deepfry`)

Applies deepfry or nuke filters to static images and GIFs. Supports attachments, direct links, replies, recent channel history, embeds, and member avatars. Includes auto-fry/auto-nuke odds, reply-only mode, and debug output.

### EmojiSteal (`emojisteal`)

Lets users steal emojis and stickers from replied-to messages, return their asset URLs, or upload them to the current server. Includes Discord context menus, `getemoji`, and mobile-friendly sticker upload flows.

![EmojiSteal example](docs/screenshots/emojisteal.png)

### GuildAssets (`guildassets`)

Owner-only backup tools for server emojis and stickers. Export a guild's current assets into the bot's data folder, then import the latest export into another server with the same bot.

### Kagi (`kagi`)

Adds Kagi Translate tools, including `linkedin` and `genz` style rewrites plus owner-only setup and auth test commands. Custom Discord emoji are normalized before style rewrites so they can be passed to Kagi cleanly.

![Kagi example](docs/screenshots/kagi.png)

### ModLog (`modlog`)

Tracks moderator actions like bans, unbans, kicks, timeout changes, and cached message edits/deletes, then posts them in a configured mod-log channel.

### No Fuck You (`nofuckyou`)

Replies with `No fuck you` when someone says `fuck you`, with configurable odds, cooldowns, thirsty mode, and tracked stats. It starts disabled until enabled with `[p]nofuckyou enable`.

![No Fuck You example](docs/screenshots/nofuckyou.png)

### SevenTV (`seventv`)

Uploads a Discord emoji from a 7TV link with `[p]7tv <link> [name]`, and inspects emotes with `[p]7tvinfo <link>`. Converts WEBP assets when needed so they can be uploaded to Discord.

### VoiceLog (`voicelog`)

Logs users joining, leaving, and moving between voice channels inside the voice channel's text chat. Includes per-event toggles and a configurable cooldown.

![VoiceLog example](docs/screenshots/voicelog.png)
