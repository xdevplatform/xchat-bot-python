from __future__ import annotations

from .bot_common import (
    build_chat_crypto,
    build_send_client,
    build_stream_client,
    decrypt_message_event,
    get_event_uuid,
    get_message_attachments,
    get_message_id,
    get_text_message,
    is_verbose,
    load_runtime_state,
    load_seen_event_uuids,
    persist_seen_event_uuids,
    send_reply,
    should_reply,
    stream_config,
)
from .state import load_state
from .util import as_dict


def main() -> None:
    env, token, user_id, private_keys, signing_key_version = load_runtime_state()
    chat = build_chat_crypto(private_keys)
    stream_client = build_stream_client(env)
    send_client = build_send_client(env, token)
    enc_key_cache: dict[str, str] = {}
    state = load_state()
    seen_event_uuids = load_seen_event_uuids(state)

    print("Listening for chat.received events...")
    for item in stream_client.activity.stream(stream_config=stream_config()):
        event = as_dict(item)
        data = event.get("data") or {}
        if data.get("event_type") != "chat.received":
            continue
        if is_verbose():
            print("chat.received event data:", as_dict(data))
        event_uuid = get_event_uuid(event, data) or "unknown"
        print(f"Received event {event_uuid}")
        if event_uuid != "unknown":
            if event_uuid in seen_event_uuids:
                print(f"Skipping duplicate event {event_uuid}")
                continue
            seen_event_uuids.add(event_uuid)
            persist_seen_event_uuids(state, seen_event_uuids)

        payload = data.get("payload") or {}
        message, conv_id, key_version = decrypt_message_event(
            chat, payload, enc_key_cache
        )
        if not message or not conv_id or not key_version:
            continue
        message_id = get_message_id(message) or "unknown"
        print(f"Received message {message_id} in {conv_id}")
        text = get_text_message(message)
        attachments = get_message_attachments(message)
        if attachments:
            print(f"Received {len(attachments)} attachment(s)")
            for attachment in attachments:
                print(f"Attachment: {attachment}")
        if text is None and not attachments:
            continue
        sender_id = message.get("sender_id") or message.get("senderId")
        if sender_id and str(sender_id) == str(user_id):
            continue
        if not should_reply(str(conv_id), text):
            continue
        reply = f"received {text}"
        send_reply(
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


if __name__ == "__main__":
    main()

