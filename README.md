# 

This is an addon for [Red Discord Bot](https://github.com/Cog-Creators/Red-DiscordBot), with features that I developed for my own servers.

These require Red 3.5, which uses the newer Discord interaction features.

### Installation

To add one of these cogs to your instance of Red, send the following commands one by one (`[p]` is your prefix):
```
[p]load downloader
[p]repo add sleepyhauru-cogs https://github.com/sleepyhauru/sleepyhauru-cogs
[p]cog install sleepyhauru-cogs [cog name]
[p]load [cog name]
```

You may be prompted to respond with "I agree" after the second command.

# Included cogs

These are the installable cogs currently present in this repo.

### ▶ AddImage (`addimage`)

Lets you save images for the bot to upload directly later, similar to aliases but for attachments. Supports guild-specific images and global bot images.

### ▶ Commands (`commands`)

Provides a single embedded command list showing categorized commands for selected installed cogs.

### ▶ Deepfry (`deepfry`)

Applies filters to images to deepfry or nuke them. Supports attached images, linked images, the most recent image, and profile pictures.

### ▶ EmojiSteal (`emojisteal`)

Lets anyone steal emojis and stickers sent by other people, and lets moderators upload them to the current server instantly. Supports context menus. Specially useful if you're on mobile as the Discord app doesn't let you copy emoji links or upload stickers, but this cog has commands for those. Animated stickers are annoying but there's a workaround.

![demonstration](https://i.imgur.com/zdizXGp.png)

### ▶ Kagi (`kagi`)

Adds Kagi Translate tools, including LinkedIn and Gen Z style transformations with random personality modes.

### ▶ No Fuck You (`nofuckyou`)

Replies with `No fuck you` when someone says `fuck you`, with configurable response odds, a per-channel cooldown, and tracked stats. It starts disabled until enabled with `[p]nofuckyou enable`.

### ▶ SevenTV (`seventv`)

Uploads a Discord emoji from a 7TV link with `[p]7tv <link> [name]`. Converts WEBP emotes to GIF or PNG when needed so they can be uploaded to Discord.

### ▶ VoiceLog (`voicelog`)

Logs users joining and leaving voicechat, inside the text chat embedded in the voicechat channel itself. Finally gives a use to those things.

![demonstration](https://i.imgur.com/U2Zitgc.png)
