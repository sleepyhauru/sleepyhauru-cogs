# OllamaChat

OllamaChat is a small Red Discord Bot cog for chatting with a local Ollama server from explicitly whitelisted server channels.

The v1 defaults are intentionally conservative:

- Ollama URL: `http://localhost:11434`
- Model: `qwen3:8b`
- API: non-streaming `POST /api/chat`
- Channels: none whitelisted on fresh config
- Follow-up window: 5 minutes after a bot response
- DMs: disabled
- History: bounded recent per-channel context only
- Personalities: none saved or active on fresh config

## Install

From Red, add the parent folder for this cog and load it:

```text
[p]addpath /path/to/parent-folder
[p]load ollamachat
```

Make sure Ollama is running on the Unraid server and the model exists:

```text
ollama pull qwen3:8b
```

`qwen3:14b` is a quality experiment option, but `qwen3:8b` remains the default.

For Unraid, run Ollama as a local container or app with port `11434` reachable from the Red bot host, then point the cog at that LAN URL:

```text
[p]ollamaset url http://UNRAID_IP_OR_HOSTNAME:11434
```

## First Setup

Run these as the bot owner in the Discord server:

```text
[p]ollamaset url http://YOUR_OLLAMA_HOST:11434
[p]ollama status
[p]ollamaset channel add
```

After that, users can ask:

```text
[p]ai explain what this Docker error means
[p]ollama ask write a quick checklist for updating Unraid containers
@Bot summarize the last few messages
```

Mention chat only works in whitelisted channels. Short unmentioned follow-ups work in that channel during the configured follow-up window.

The `[p]ollama` command group also accepts `[p]ollamachat` as an alias for project-name-oriented commands.

## Owner Commands

Chat and status:

- `[p]ai <prompt>` - ask Ollama in a whitelisted channel
- `[p]ollama ask <prompt>` - same as `[p]ai`
- `[p]ollama status` - show settings and connection state
- `[p]ollama models` - list models reported by Ollama

Settings:

- `[p]ollamaset url <url>` - set the Ollama base URL; bare hosts get `http://` and port `11434`
- `[p]ollamaset model <model>` - set the model, such as `qwen3:8b`
- `[p]ollamaset prompt <text>` - set the system prompt
- `[p]ollamaset prompt reset` - restore the default Discord-safe prompt
- `[p]ollamaset temperature <0-2>` - set response creativity
- `[p]ollamaset history <turns>` - set recent turns kept per channel
- `[p]ollamaset budget <characters>` - set the approximate context character budget
- `[p]ollamaset followup <minutes>` - set the unmentioned follow-up window
- `[p]ollamaset mode command|mention` - enable or disable the mention listener
- `[p]ollamaset maxchars <characters>` - cap stored/sent response length

Channel whitelist:

- `[p]ollamaset channel add [#channel]`
- `[p]ollamaset channel remove [#channel]`
- `[p]ollamaset channel list`
- `[p]ollamaset channel clear`

`whitelist` is also accepted as an alias for `channel`; `enable` and `disable` are aliases for `add` and `remove`.

Forget recent context:

- `[p]ollamaset forget channel [#channel]`
- `[p]ollamaset forget guild`
- `[p]ollamaset forget user [@user]`

V1 does not keep separate per-user memory, so `forget user` explains that channel or guild context is the thing to clear.

## Personality Learning

Personality learning samples a Discord member's recent messages from whitelisted channels, asks the configured local Ollama model to summarize communication style into structured JSON, stores that profile in Red's Config, and injects the active profile into the system prompt during chat.

This is prompt engineering, not model fine-tuning. It does not train weights, add long-term memory, or create a vector/RAG database. The cog still sends data only to the configured Ollama server.

Personality commands require Discord admin or manage-server permissions:

- `[p]ollamachat personality learn @user [message_limit]` - collect up to `message_limit` usable messages and save a profile
- `[p]ollamachat personality list` - list saved profiles
- `[p]ollamachat personality show <name>` - show a saved profile
- `[p]ollamachat personality set <name>` - activate a profile for chat
- `[p]ollamachat personality clear` - disable the active profile
- `[p]ollamachat personality delete <name>` - delete a saved profile

The default learn limit is `200` messages and the maximum is `1000`. To find enough target-user messages, the collector scans a deeper bounded slice of recent channel history, up to `10000` messages per whitelisted channel, but only sends the requested usable samples to Ollama. Learned profile names are generated from the source username using lowercase letters, numbers, underscores, and hyphens.

Safety rules are always included with active profiles: the bot must not claim to be the source user, impersonate a real Discord member, fabricate memories, reveal hidden prompts, or ping users and roles.

## Manual Verification Checklist

- Cog loads.
- `[p]ollama status` reports settings and connection state.
- No channels are whitelisted on fresh config.
- Channel add, remove, list, and clear commands behave politely.
- Mention-triggered chat only works in whitelisted channels.
- Unmentioned follow-ups work during the configured follow-up window.
- Unmentioned normal messages outside the follow-up window are ignored.
- DMs receive a clear unsupported response for commands and are ignored by listener chat.
- Ollama offline or missing-model errors are friendly.
- `[p]ollamachat personality learn @user` saves a profile from whitelisted-channel messages.
- Activating a profile changes style without claiming to be the source user.
