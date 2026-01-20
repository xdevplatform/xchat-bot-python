from __future__ import annotations

import getpass
import json

from chat_xdk import Chat
from xdk import Client

from .env import load_env
from .state import load_state, save_state


def _as_dict(obj) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return dict(obj)
    except Exception:
        return {}


def _get_public_keys(client: Client, user_id: str, fields: list[str]) -> dict:
    token = client.access_token or (client.token or {}).get("access_token")
    if not token:
        raise SystemExit("Missing OAuth2 access token")
    url = f"{client.base_url}/2/users/{user_id}/public_keys"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"public_key.fields": ",".join(fields)}
    resp = client.session.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    env = load_env()
    state = load_state()
    token = state.get("oauth_token")
    if not token:
        raise SystemExit("Missing oauth_token in state.json. Run xchat-bot-login first.")

    client = Client(
        base_url=env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        token=token,
        client_id=env.get("OAUTH_CLIENT_ID"),
        client_secret=env.get("OAUTH_CLIENT_SECRET"),
        redirect_uri=env.get("OAUTH_REDIRECT_URI"),
        scope=env.get("OAUTH_SCOPES"),
    )
    client.session.headers["X-B3-Flags"] = "1"
    client.session.headers["X-TFE-Experiment-environment"] = "staging1"
    me = _as_dict(client.users.get_me())
    user_id = (me.get("data") or {}).get("id")
    if not user_id:
        raise SystemExit("Could not resolve user id from /2/users/me")

    fields = ["version", "public_key", "signing_public_key", "juicebox_config"]
    pk = _get_public_keys(client, str(user_id), fields)
    data = pk.get("data") or {}
    juicebox_config = data.get("juicebox_config")
    if isinstance(juicebox_config, dict):
        token_map = juicebox_config.get("token_map") or []
        tokens = {
            entry.get("key"): (entry.get("value") or {}).get("token")
            for entry in token_map
            if entry.get("key") and (entry.get("value") or {}).get("token")
        }
        key_store_json = juicebox_config.get("key_store_token_map_json")
        if key_store_json and tokens:
            config_obj = {
                "sdk_config": key_store_json,
                "tokens": tokens,
                "max_guess_count": juicebox_config.get("max_guess_count", 20),
            }
            juicebox_config = json.dumps(config_obj)
        else:
            juicebox_config = json.dumps(juicebox_config)
    signing_key_version = data.get("version") or ""
    if not juicebox_config:
        raise SystemExit("Missing juicebox_config in public keys response")

    pin = env.get("XCHAT_PIN") or getpass.getpass("XChat PIN: ")
    chat = Chat()
    chat.unlock(pin, juicebox_config)
    private_keys = chat.export_keys()
    if not private_keys:
        raise SystemExit("Unlock succeeded but no private keys were exported")

    state.update(
        {
            "user_id": str(user_id),
            "private_keys": private_keys,
            "signing_key_version": str(signing_key_version),
            "juicebox_config": juicebox_config,
        }
    )
    save_state(state)
    print("Saved private keys and signing key version to state.json")


if __name__ == "__main__":
    main()

