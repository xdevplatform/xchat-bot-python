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


## Step 3: Subscribe to Activity Stream

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

## Notes

- `state.json` contains tokens and keys. Keep it local and uncommitted.
- You can override any `.env` value with environment variables.

