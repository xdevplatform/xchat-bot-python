from __future__ import annotations

from chat_xdk import Chat


def as_dict(obj) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return dict(obj)
    except Exception:
        return {}


def pick_decryptable_key(
    chat: Chat, key_change_event: str | None
) -> tuple[str | None, str | None]:
    if not key_change_event:
        return None, None
    evt = as_dict(chat.decrypt_event(key_change_event, ""))
    if evt.get("type") != "KeyChange":
        return None, None
    for pk in evt.get("participant_keys") or []:
        enc = pk.get("encrypted_key")
        if not enc:
            continue
        try:
            chat.decrypt_conversation_key(enc)
            return enc, evt.get("key_version")
        except Exception:
            continue
    return None, evt.get("key_version")

