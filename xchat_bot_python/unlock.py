from __future__ import annotations

import getpass
import json
import logging

from chat_xdk import Chat
from xdk import Client

from .env import load_env
from .state import load_state, save_state
from .util import redact_secret, truthy_env


def _as_dict(obj) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return dict(obj)
    except Exception:
        return {}


logger = logging.getLogger("xchat_bot")


def _configure_logging(env: dict) -> None:
    # Enable with XCHAT_DEBUG=1
    level = logging.DEBUG if truthy_env(env.get("XCHAT_DEBUG")) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _summarize_juicebox_config(juicebox_config: str) -> dict:
    """Parse juicebox_config JSON and return a safe-to-log summary."""
    try:
        outer = json.loads(juicebox_config)
    except Exception:
        return {"parse_error": "invalid_json"}
    summary: dict = {
        "max_guess_count": outer.get("max_guess_count"),
        "token_realm_count": len((outer.get("tokens") or {}).keys()),
    }
    sdk_cfg_raw = outer.get("sdk_config")
    if isinstance(sdk_cfg_raw, str):
        try:
            sdk_cfg = json.loads(sdk_cfg_raw)
            realms = sdk_cfg.get("realms") or []
            summary.update(
                {
                    "realm_count": len(realms),
                    "realm_addresses": [
                        r.get("address") for r in realms if isinstance(r, dict)
                    ],
                    "register_threshold": sdk_cfg.get("register_threshold"),
                    "recover_threshold": sdk_cfg.get("recover_threshold"),
                    "pin_hashing_mode": sdk_cfg.get("pin_hashing_mode"),
                }
            )
        except Exception:
            summary["sdk_config_parse_error"] = "invalid_json"
    return summary


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
    _configure_logging(env)
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

    logger.info(
        "unlock config send_base_url=%s oauth_access_token=%s user_id=%s signing_key_version=%s",
        env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        redact_secret((token or {}).get("access_token")),
        user_id,
        signing_key_version,
    )
    if truthy_env(env.get("XCHAT_DEBUG")):
        logger.debug(
            "unlock juicebox_config_summary=%s",
            json.dumps(_summarize_juicebox_config(juicebox_config)),
        )

    pin = env.get("XCHAT_PIN") or getpass.getpass("XChat PIN: ")
    chat = Chat()
    try:
        chat.unlock(pin, juicebox_config)
    except Exception as e:
        # chat_xdk surfaces Juicebox failures as ValueError with message text.
        logger.error("unlock failed: %r", e)
        msg = str(e)
        if "Rate limit exceeded" in msg:
            logger.error(
                "Juicebox rate limit hit. Wait before retrying to avoid extending cooldown."
            )
        raise
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

