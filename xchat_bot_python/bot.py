from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from chat_xdk import Chat
from xdk import Client
from xdk.chat.models import SendMessageRequest
from xdk.streaming import StreamConfig, StreamError

from .env import load_env
from .state import load_state
from .util import as_dict, pick_decryptable_key, redact_secret, truthy_env


STAGING_HEADERS = {
    "X-TFE-Experiment-environment": "staging1",
    "dtab-local": (
        "/s/datadelivery-staf/proxyapp-endpoint-ActivityStream:https => "
        "/srv#/staging1/atla/datadelivery-staf/proxyapp-endpoint-ActivityStream:https"
    ),
}


logger = logging.getLogger("xchat_bot")


def _configure_logging(env: dict) -> None:
    # Enable with XCHAT_DEBUG=1
    level = logging.DEBUG if truthy_env(env.get("XCHAT_DEBUG")) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_chat_crypto(private_keys: str) -> Chat:
    # Chat-xdk handles decrypt/encrypt with your private keys.
    chat = Chat()
    chat.import_keys(private_keys)
    return chat


def _build_stream_client(env: dict) -> Client:
    client = Client(
        base_url=env.get("XDK_BASE_URL", "https://global.dev.cftls.t.co"),
        bearer_token=env.get("BEARER_TOKEN"),
    )
    client.session.headers.update(STAGING_HEADERS)
    return client


def _build_send_client(env: dict, token: dict) -> Client:
    client = Client(
        base_url=env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        token=token,
        client_id=env.get("OAUTH_CLIENT_ID"),
        client_secret=env.get("OAUTH_CLIENT_SECRET"),
        redirect_uri=env.get("OAUTH_REDIRECT_URI"),
        scope=env.get("OAUTH_SCOPES"),
    )
    client.session.headers.update(STAGING_HEADERS)
    return client


def _stream_config() -> StreamConfig:
    return StreamConfig(
        max_retries=-1,
        on_connect=lambda: print("Connected to activity stream"),
        on_disconnect=lambda exc=None: print(f"Disconnected: {exc!r}"),
        on_reconnect=lambda attempt, delay: print(
            f"Reconnecting attempt={attempt} in {delay:.1f}s"
        ),
        on_error=lambda err: logger.error(
            "stream_error type=%s status=%s body=%s",
            getattr(err, "error_type", None),
            getattr(err, "status_code", None),
            (getattr(err, "response_body", None) or "")[:2000],
        ),
    )


def _get_text_message(event: dict) -> Optional[str]:
    if event.get("type") != "Message":
        return None
    content = event.get("content") or {}
    if content.get("content_type") != "Text":
        return None
    return content.get("text") or ""


def _send_reply(
    *,
    chat: Chat,
    send_client: Client,
    user_id: str,
    signing_key_version: str,
    conv_id: str,
    conv_token: Optional[str],
    enc_key: str,
    key_version: str,
    reply: str,
) -> None:
    # Build encrypted message + signature for the API.
    msg_id = str(uuid.uuid4())
    payload = chat.encrypt_message_for_api(
        msg_id,
        str(user_id),
        str(conv_id),
        enc_key,
        reply,
        str(key_version),
        str(signing_key_version),
    )
    request_data = {
        "message_id": msg_id,
        "encoded_message_create_event": payload.encrypted_content,
        "encoded_message_event_signature": payload.encoded_event_signature,
    }
    if conv_token:
        request_data["conversation_token"] = conv_token
    req = SendMessageRequest.model_validate(request_data)
    send_client.chat.send_message(conv_id.replace(":", "-"), req)


def main() -> None:
    env = load_env()
    _configure_logging(env)
    state = load_state()

    token = state.get("oauth_token")
    user_id = state.get("user_id")
    private_keys = state.get("private_keys")
    signing_key_version = state.get("signing_key_version")
    if not all([token, user_id, private_keys, signing_key_version]):
        raise SystemExit("Missing state.json data. Run login and unlock first.")

    chat = _build_chat_crypto(private_keys)
    stream_client = _build_stream_client(env)
    send_client = _build_send_client(env, token)
    enc_key_cache: dict[str, str] = {}

    # High-signal diagnostics (no secrets).
    logger.info(
        "config stream_base_url=%s bearer_token=%s",
        env.get("XDK_BASE_URL", "https://global.dev.cftls.t.co"),
        redact_secret(env.get("BEARER_TOKEN")),
    )
    logger.info(
        "config send_base_url=%s oauth_access_token=%s user_id=%s signing_key_version=%s",
        env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        redact_secret((token or {}).get("access_token")),
        user_id,
        signing_key_version,
    )
    if truthy_env(env.get("XCHAT_DEBUG")):
        logger.debug("staging_headers=%s", json.dumps(STAGING_HEADERS))

    print("Listening for chat.received events...")
    try:
        for item in stream_client.activity.stream(stream_config=_stream_config()):
            event = as_dict(item)
            data = event.get("data") or {}
            if data.get("event_type") != "chat.received":
                continue

            payload = data.get("payload") or {}
            conv_id = payload.get("conversation_id")
            encoded_event = payload.get("encoded_event")
            if not conv_id or not encoded_event:
                continue

            # Get the encrypted conversation key for this bot.
            enc_key = payload.get("encrypted_conversation_key")
            key_version = payload.get("conversation_key_version")
            if not enc_key:
                enc_key, key_version = pick_decryptable_key(
                    chat, payload.get("conversation_key_change_event")
                )
            if not enc_key or not key_version:
                continue
            enc_key_cache[conv_id] = enc_key

            # Decrypt and reply to text messages only.
            message = as_dict(chat.decrypt_event(encoded_event, enc_key))
            text = _get_text_message(message)
            if text is None:
                continue

            reply = f"got it: {text}"
            _send_reply(
                chat=chat,
                send_client=send_client,
                user_id=str(user_id),
                signing_key_version=str(signing_key_version),
                conv_id=str(conv_id),
                conv_token=payload.get("conversation_token"),
                enc_key=enc_key_cache[conv_id],
                key_version=str(key_version),
                reply=reply,
            )
            print(f"Replied to {conv_id}: {reply}")
    except StreamError as e:
        # Print as much as possible about the failing HTTP call.
        logger.error(
            "Activity stream failed: type=%s status=%s message=%s",
            getattr(e, "error_type", None),
            getattr(e, "status_code", None),
            str(e),
        )
        if getattr(e, "response_body", None):
            logger.error("Activity stream response body: %s", e.response_body)
        raise


if __name__ == "__main__":
    main()

