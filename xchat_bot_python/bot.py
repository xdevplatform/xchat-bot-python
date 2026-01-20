from __future__ import annotations

import uuid

from chat_xdk import Chat
from xdk import Client
from xdk.chat.models import SendMessageRequest
from xdk.streaming import StreamConfig

from .env import load_env
from .state import load_state
from .util import as_dict, pick_decryptable_key


def main() -> None:
    env = load_env()
    state = load_state()
    token = state.get("oauth_token")
    user_id = state.get("user_id")
    private_keys = state.get("private_keys")
    signing_key_version = state.get("signing_key_version")
    if not all([token, user_id, private_keys, signing_key_version]):
        raise SystemExit("Missing state.json data. Run login and unlock first.")

    chat = Chat()
    chat.import_keys(private_keys)
    stream_client = Client(
        base_url=env.get("XDK_BASE_URL", "https://global.dev.cftls.t.co"),
        bearer_token=env.get("BEARER_TOKEN"),
    )
    stream_client.session.headers["X-TFE-Experiment-environment"] = "staging1"
    stream_client.session.headers[
        "dtab-local"
    ] = (
        "/s/datadelivery-staf/proxyapp-endpoint-ActivityStream:https => "
        "/srv#/staging1/atla/datadelivery-staf/proxyapp-endpoint-ActivityStream:https"
    )
    send_client = Client(
        base_url=env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
        token=token,
        client_id=env.get("OAUTH_CLIENT_ID"),
        client_secret=env.get("OAUTH_CLIENT_SECRET"),
        redirect_uri=env.get("OAUTH_REDIRECT_URI"),
        scope=env.get("OAUTH_SCOPES"),
    )
    send_client.session.headers["X-TFE-Experiment-environment"] = "staging1"
    send_client.session.headers[
        "dtab-local"
    ] = (
        "/s/datadelivery-staf/proxyapp-endpoint-ActivityStream:https => "
        "/srv#/staging1/atla/datadelivery-staf/proxyapp-endpoint-ActivityStream:https"
    )

    enc_key_cache: dict[str, str] = {}
    print("Listening for chat.received events...")
    stream_config = StreamConfig(
        max_retries=-1,
        on_connect=lambda: print("Connected to activity stream"),
        on_disconnect=lambda exc=None: print(f"Disconnected: {exc!r}"),
        on_reconnect=lambda attempt, delay: print(
            f"Reconnecting attempt={attempt} in {delay:.1f}s"
        ),
    )
    for item in stream_client.activity.stream(stream_config=stream_config):
        event = as_dict(item)
        data = event.get("data") or {}
        if data.get("event_type") != "chat.received":
            continue
        payload = data.get("payload") or {}
        conv_id = payload.get("conversation_id")
        encoded_event = payload.get("encoded_event")
        if not conv_id or not encoded_event:
            continue
        enc_key = payload.get("encrypted_conversation_key")
        key_version = payload.get("conversation_key_version")
        if not enc_key:
            enc_key, key_version = pick_decryptable_key(
                chat, payload.get("conversation_key_change_event")
            )
        if not enc_key:
            continue
        enc_key_cache[conv_id] = enc_key
        message = as_dict(chat.decrypt_event(encoded_event, enc_key))
        if message.get("type") != "Message":
            continue
        content = message.get("content") or {}
        if content.get("content_type") != "Text":
            continue
        text = content.get("text") or ""
        reply = f"got it: {text}"
        send_key_version = key_version or message.get("key_version")
        if not send_key_version:
            continue
        msg_id = str(uuid.uuid4())
        send_payload = chat.encrypt_message_for_api(
            msg_id,
            str(user_id),
            str(conv_id),
            enc_key_cache[conv_id],
            reply,
            str(send_key_version),
            str(signing_key_version),
        )
        request_data = {
            "message_id": msg_id,
            "encoded_message_create_event": send_payload.encrypted_content,
            "encoded_message_event_signature": send_payload.encoded_event_signature,
        }
        if payload.get("conversation_token"):
            request_data["conversation_token"] = payload.get("conversation_token")
        req = SendMessageRequest.model_validate(request_data)
        send_client.chat.send_message(conv_id.replace(":", "-"), req)
        print(f"Replied to {conv_id}: {reply}")


if __name__ == "__main__":
    main()

