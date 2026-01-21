## xchat-bot-python

Minimal X Chat bot example with a login, unlock, and run flow. Everything the bot
needs (tokens, keys, env) lives in this directory.

## Directory layout

This example expects the XDK repos to be sibling directories:

```
<parent>/
  xdk-python/
  chat-xdk/
  xchat-bot-python/
```

## Requirements

- Python 3.10+
- `uv`
- X app credentials (OAuth2 client id/secret)
- Activity Stream bearer token

## Setup

From the parent directory:

```bash
cd xchat-bot-python
cp env.template .env
```

Edit `.env` with:

- `BEARER_TOKEN`
- `OAUTH_CLIENT_ID`
- `OAUTH_CLIENT_SECRET`
- `OAUTH_REDIRECT_URI`
- `OAUTH_SCOPES`

Install dependencies:

```bash
uv sync
```

## Step 1: Login (OAuth2)

Run the login command and follow the prompt:

```bash
uv run xchat-bot-login
```

This stores the OAuth2 token in `state.json`.

## Step 2: Unlock private keys

Fetch public keys, prompt for PIN, and store private keys locally:

```bash
uv run xchat-bot-unlock
```

This uses `/2/users/:id/public_keys` and stores:

- `private_keys`
- `signing_key_version`
- `user_id`

All are saved in `state.json`.


## Step 3: Subscribe to Activity Stream (DOESN'T EXIST YET IN PUBLIC_API)

Create a `chat.received` subscription for the authenticated user:

```bash
xurl -X POST --auth oauth2 "/2/activity/subscriptions" -d \
  '{"event_type": "chat.received", "filter": {"user_id": "{id}"}, "tag": "bot received messages"}'
```

## Step 4: Run the bot

```bash
uv run xchat-bot-run
```

The bot connects to the Activity Stream using `BEARER_TOKEN`, decrypts incoming
messages using `private_keys`, and replies using the OAuth2 user token.

## Writing commands (decorators)

This project supports a Discord.py-like decorator style for commands/events. You
can create a separate bot module and register handlers (see
`xchat_bot_python/decorator_example_bot.py`):

```python
from xchat_bot_python.decorators_bot import Context, XChatBot

bot = XChatBot(command_prefix="!")

@bot.command("ping")
async def ping(ctx: Context) -> None:
    await ctx.reply_async("pong")

@bot.command("echo")
def echo(ctx: Context) -> None:
    ctx.reply(" ".join(ctx.args))

@bot.event
def on_message(ctx: Context) -> None:
    # Runs for every decrypted Text message (commands and non-commands).
    ...

bot.run()
```

Notes:

- Commands are **prefix-based**. With `command_prefix="!"`, a message like
  `!echo hello world` maps to command `echo` with args `["hello", "world"]`.
- Handlers can be either `def` or `async def`.
- Use `ctx.reply(...)` (sync) or `await ctx.reply_async(...)` (async).

To run the decorator example:

```bash
uv run python -m xchat_bot_python.decorator_example_bot
```

## Notes

- `state.json` contains tokens and keys. Keep it local and uncommitted.
- You can override any `.env` value with environment variables.

