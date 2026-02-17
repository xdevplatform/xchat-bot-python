from __future__ import annotations

import json
import sys
import uuid
from typing import Optional

from chat_xdk import Chat
from xdk import Client
from xdk.chat.models import SendMessageRequest
from xdk.streaming import StreamConfig

from .env import load_env
from .state import load_state, save_state
from .util import as_dict, pick_decryptable_key


GROUP_MENTION = "@mr_the2nd"
DEDUPE_MAX_EVENTS = 500


def build_chat_crypto(private_keys: str) -> Chat:
    chat = Chat()
    chat.import_keys(private_keys)
    return chat


def build_stream_client(env: dict) -> Client:
    client = Client(
        base_url=env.get("XDK_BASE_URL", "https://api.x.com"),
        bearer_token=env.get("BEARER_TOKEN"),
    )
    return client


def build_send_client(env: dict, token: dict) -> Client:
    client = Client(
        base_url=env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        token=token,
        client_id=env.get("OAUTH_CLIENT_ID"),
        client_secret=env.get("OAUTH_CLIENT_SECRET"),
        redirect_uri=env.get("OAUTH_REDIRECT_URI"),
        scope=env.get("OAUTH_SCOPES"),
    )
    return client


def is_verbose() -> bool:
    return "--verbose" in sys.argv


def stream_config() -> StreamConfig:
    return StreamConfig(
        max_retries=-1,
        on_connect=lambda: print("Connected to activity stream"),
        on_disconnect=lambda exc=None: print(f"Disconnected: {exc!r}"),
        on_reconnect=lambda attempt, delay: print(
            f"Reconnecting attempt={attempt} in {delay:.1f}s"
        ),
    )


def get_text_message(event: dict) -> Optional[str]:
    if event.get("type") != "Message":
        return None
    content = event.get("content") or {}
    if content.get("content_type") != "Text":
        return None
    return content.get("text") or ""


def get_message_attachments(event: dict) -> list[dict]:
    if event.get("type") != "Message":
        return []
    content = event.get("content") or {}
    attachments = content.get("attachments") or []
    if isinstance(attachments, list):
        return [att for att in attachments if isinstance(att, dict)]
    return []


def get_message_id(event: dict) -> Optional[str]:
    return (
        event.get("message_id")
        or event.get("messageId")
        or event.get("id")
        or event.get("message", {}).get("id")
    )


def get_message_sequence_id(event: dict) -> Optional[str]:
    return (
        event.get("sequence_id")
        or event.get("sequenceId")
        or event.get("message", {}).get("sequence_id")
        or event.get("message", {}).get("sequenceId")
    )


def get_event_uuid(event: dict, data: dict) -> Optional[str]:
    return (
        data.get("event_uuid")
        or event.get("event_uuid")
        or data.get("event_id")
        or event.get("event_id")
        or data.get("id")
        or event.get("id")
    )


def load_seen_event_uuids(state: dict) -> set[str]:
    return set(state.get("seen_event_uuids", []))


def persist_seen_event_uuids(state: dict, seen: set[str]) -> None:
    if len(seen) > DEDUPE_MAX_EVENTS:
        seen = set(list(seen)[-DEDUPE_MAX_EVENTS:])
    state["seen_event_uuids"] = sorted(seen)
    save_state(state)


def should_reply(conv_id: str, text: str) -> bool:
    if conv_id.startswith("g"):
        return GROUP_MENTION in text
    return True


def send_reply(
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


def send_reaction(
    *,
    chat: Chat,
    send_client: Client,
    user_id: str,
    signing_key_version: str,
    conv_id: str,
    conv_token: Optional[str],
    enc_key: str,
    key_version: str,
    target_message_sequence_id: str,
    emoji: str,
    remove: bool,
) -> None:
    if not hasattr(chat, "encrypt_reaction_add_for_api"):
        raise RuntimeError(
            "chat_xdk is missing Chat.encrypt_reaction_add_for_api(); rebuild/reinstall chat-xdk"
        )

    reaction_id = str(uuid.uuid4())
    if remove:
        payload = chat.encrypt_reaction_remove_for_api(
            reaction_id,
            str(user_id),
            str(conv_id),
            enc_key,
            target_message_sequence_id,
            emoji,
            str(key_version),
            str(signing_key_version),
        )
    else:
        payload = chat.encrypt_reaction_add_for_api(
            reaction_id,
            str(user_id),
            str(conv_id),
            enc_key,
            target_message_sequence_id,
            emoji,
            str(key_version),
            str(signing_key_version),
        )
    request_data = {
        "message_id": reaction_id,
        "encoded_message_create_event": payload.encrypted_content,
        "encoded_message_event_signature": payload.encoded_event_signature,
    }
    if conv_token:
        request_data["conversation_token"] = conv_token
    req = SendMessageRequest.model_validate(request_data)
    send_client.chat.send_message(conv_id.replace(":", "-"), req)


def load_runtime_state() -> tuple[dict, dict, str, str, str]:
    env = load_env()
    state = load_state()
    token = state.get("oauth_token")
    user_id = state.get("user_id")
    private_keys = state.get("private_keys")
    signing_key_version = state.get("signing_key_version")
    if not all([token, user_id, private_keys, signing_key_version]):
        raise SystemExit("Missing state.json data. Run login and unlock first.")
    return env, token, str(user_id), str(private_keys), str(signing_key_version)


def decrypt_message_event(
    chat: Chat,
    payload: dict,
    enc_key_cache: dict[str, str],
) -> tuple[dict, str | None, str | None]:
    conv_id = payload.get("conversation_id")
    encoded_event = payload.get("encoded_event")
    if not conv_id or not encoded_event:
        return {}, None, None

    enc_key = payload.get("encrypted_conversation_key")
    key_version = payload.get("conversation_key_version")
    if not enc_key:
        enc_key, key_version = pick_decryptable_key(
            chat, payload.get("conversation_key_change_event")
        )
    if not enc_key or not key_version:
        return {}, None, None
    enc_key_cache[conv_id] = enc_key

    message = as_dict(chat.decrypt_event(encoded_event, enc_key))
    if is_verbose():
        print(
            "Decoded message event:",
            json.dumps(message, indent=2, sort_keys=True, default=str),
        )
    return message, str(conv_id), str(key_version)
