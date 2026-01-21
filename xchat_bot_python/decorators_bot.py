from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from chat_xdk import Chat
from xdk import Client
from xdk.chat.models import SendMessageRequest
from xdk.streaming import StreamConfig, StreamError

from .env import load_env
from .state import load_state
from .util import as_dict, pick_decryptable_key, redact_secret, truthy_env


Handler = Callable[["Context"], Any]
EventHandler = Callable[..., Any]


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


def _summarize_payload(payload: dict) -> dict[str, Any]:
    """
    Safe-to-log payload summary (no secrets, no encoded blobs).

    Avoid logging:
    - encoded_event
    - encrypted_conversation_key
    - conversation_key_change_event
    - conversation_token
    """

    return {
        "has_conversation_id": bool(payload.get("conversation_id")),
        "has_encoded_event": bool(payload.get("encoded_event")),
        "has_conversation_token": bool(payload.get("conversation_token")),
        "has_encrypted_conversation_key": bool(payload.get("encrypted_conversation_key")),
        "has_conversation_key_change_event": bool(payload.get("conversation_key_change_event")),
        "conversation_key_version": payload.get("conversation_key_version"),
        "payload_keys": sorted([k for k in payload.keys() if isinstance(k, str)])[:50],
    }


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
    request_data: dict[str, Any] = {
        "message_id": msg_id,
        "encoded_message_create_event": payload.encrypted_content,
        "encoded_message_event_signature": payload.encoded_event_signature,
    }
    if conv_token:
        request_data["conversation_token"] = conv_token
    req = SendMessageRequest.model_validate(request_data)
    send_client.chat.send_message(conv_id.replace(":", "-"), req)


def _is_coro_fn(fn: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(fn)


def _safe_create_task(loop: asyncio.AbstractEventLoop, coro: Awaitable[Any]) -> None:
    # Fire-and-forget while still surfacing exceptions to logs.
    fut = asyncio.run_coroutine_threadsafe(coro, loop)

    def _log_done(f: "asyncio.Future[Any]") -> None:
        try:
            f.result()
        except Exception:
            logger.exception("handler_task_failed")

    fut.add_done_callback(_log_done)  # type: ignore[arg-type]


class _AsyncLoopThread:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = threading.Event()

    def start(self) -> asyncio.AbstractEventLoop:
        if self._loop and self._loop.is_running():
            return self._loop

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._started.set()
            loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="xchat_bot_asyncio")
        self._thread.start()
        self._started.wait(timeout=5)
        if not self._loop:
            raise RuntimeError("Failed to start asyncio loop thread")
        return self._loop


@dataclass(frozen=True)
class Context:
    bot: "XChatBot"
    conv_id: str
    conv_token: Optional[str]
    enc_key: str
    key_version: str
    payload: dict
    message: dict
    text: str
    command: Optional[str]
    args: list[str]

    def reply(self, text: str) -> None:
        logger.debug(
            "send_reply conv_id=%s text_len=%d",
            self.conv_id,
            len(text or ""),
        )
        _send_reply(
            chat=self.bot._chat,
            send_client=self.bot._send_client,
            user_id=str(self.bot._user_id),
            signing_key_version=str(self.bot._signing_key_version),
            conv_id=str(self.conv_id),
            conv_token=self.conv_token,
            enc_key=self.enc_key,
            key_version=str(self.key_version),
            reply=text,
        )

    async def reply_async(self, text: str) -> None:
        await asyncio.to_thread(self.reply, text)


class XChatBot:
    """
    Discord.py-like decorator interface on top of the existing XChat bot plumbing.

    - Prefix commands: "!ping hello" -> command="ping", args=["hello"]
    - Sync + async handlers supported.
    """

    def __init__(self, *, command_prefix: str = "!") -> None:
        self.command_prefix = command_prefix
        self._commands: dict[str, Handler] = {}
        self._events: dict[str, list[EventHandler]] = {}
        self._async_thread = _AsyncLoopThread()
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None

        # Initialized in setup()
        self._env: dict[str, str] = {}
        self._state: dict[str, Any] = {}
        self._chat: Chat
        self._stream_client: Client
        self._send_client: Client
        self._enc_key_cache: dict[str, str] = {}
        self._user_id: str
        self._signing_key_version: str

    # -------------------------
    # Decorators / registration
    # -------------------------
    def command(
        self, name: str | None = None, *, aliases: Optional[list[str]] = None
    ) -> Callable[[Handler], Handler]:
        aliases = aliases or []

        def _decorator(fn: Handler) -> Handler:
            cmd_name = (name or getattr(fn, "__name__", "")).strip()
            if not cmd_name:
                raise ValueError("Command must have a name")
            self._commands[cmd_name.lower()] = fn
            for a in aliases:
                if a:
                    self._commands[a.lower()] = fn
            logger.debug(
                "registered_command name=%s aliases=%s handler=%s",
                cmd_name.lower(),
                [a.lower() for a in aliases if a],
                getattr(fn, "__name__", repr(fn)),
            )
            return fn

        return _decorator

    def event(self, fn: EventHandler) -> EventHandler:
        event_name = getattr(fn, "__name__", "").strip()
        if not event_name:
            raise ValueError("Event handler must have a function name")
        self._events.setdefault(event_name, []).append(fn)
        logger.debug(
            "registered_event name=%s handler=%s",
            event_name,
            getattr(fn, "__name__", repr(fn)),
        )
        return fn

    # -------------
    # Core runtime
    # -------------
    def preflight(self) -> None:
        """
        Validate configuration early and emit actionable logs.

        This does not make network calls; it just checks local env/state shape.
        """

        missing_env: list[str] = []
        if not self._env.get("BEARER_TOKEN"):
            missing_env.append("BEARER_TOKEN")
        if not self._env.get("OAUTH_CLIENT_ID"):
            missing_env.append("OAUTH_CLIENT_ID")
        if not self._env.get("OAUTH_CLIENT_SECRET"):
            missing_env.append("OAUTH_CLIENT_SECRET")
        if not self._env.get("OAUTH_REDIRECT_URI"):
            missing_env.append("OAUTH_REDIRECT_URI")
        if missing_env:
            logger.warning("missing_env_vars=%s", ",".join(missing_env))

        token = self._state.get("oauth_token") or {}
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not access_token:
            logger.error("missing_oauth_access_token_in_state")
        if not self._state.get("user_id"):
            logger.error("missing_user_id_in_state")
        if not self._state.get("private_keys"):
            logger.error("missing_private_keys_in_state")
        if not self._state.get("signing_key_version"):
            logger.error("missing_signing_key_version_in_state")

    def setup(self) -> None:
        self._env = load_env()
        _configure_logging(self._env)
        self._state = load_state()
        self.preflight()

        token = self._state.get("oauth_token")
        self._user_id = self._state.get("user_id")
        private_keys = self._state.get("private_keys")
        self._signing_key_version = self._state.get("signing_key_version")
        if not all([token, self._user_id, private_keys, self._signing_key_version]):
            raise SystemExit("Missing state.json data. Run login and unlock first.")

        self._chat = _build_chat_crypto(private_keys)
        self._stream_client = _build_stream_client(self._env)
        self._send_client = _build_send_client(self._env, token)
        self._enc_key_cache = {}

        # High-signal diagnostics (no secrets).
        logger.info(
            "config stream_base_url=%s bearer_token=%s",
            self._env.get("XDK_BASE_URL", "https://global.dev.cftls.t.co"),
            redact_secret(self._env.get("BEARER_TOKEN")),
        )
        logger.info(
            "config send_base_url=%s oauth_access_token=%s user_id=%s signing_key_version=%s",
            self._env.get("XCHAT_SEND_BASE_URL", "https://api.x.com"),
            redact_secret((token or {}).get("access_token")),
            self._user_id,
            self._signing_key_version,
        )
        logger.info(
            "bot_ready command_prefix=%r command_count=%d",
            self.command_prefix,
            len({k: v for k, v in self._commands.items() if v}),
        )

    def _ensure_async_loop(self) -> asyncio.AbstractEventLoop:
        if self._async_loop and self._async_loop.is_running():
            return self._async_loop
        self._async_loop = self._async_thread.start()
        return self._async_loop

    def _emit(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        handlers = self._events.get(event_name, [])
        if not handlers:
            return
        loop = self._ensure_async_loop()
        for fn in handlers:
            try:
                logger.debug(
                    "dispatch_event event=%s handler=%s",
                    event_name,
                    getattr(fn, "__name__", repr(fn)),
                )
                if _is_coro_fn(fn):
                    _safe_create_task(loop, fn(*args, **kwargs))  # type: ignore[misc]
                else:
                    fn(*args, **kwargs)
            except Exception:
                logger.exception("event_handler_failed event=%s handler=%r", event_name, fn)

    def _parse_command(self, text: str) -> tuple[Optional[str], list[str]]:
        prefix = self.command_prefix or ""
        if not prefix or not text.startswith(prefix):
            return None, []
        body = text[len(prefix) :].strip()
        if not body:
            return None, []
        parts = body.split()
        return parts[0].lower(), parts[1:]

    def _dispatch_context(self, ctx: Context) -> None:
        # 1) Always emit on_message if present (Discord-ish behavior).
        self._emit("on_message", ctx)

        # 2) If it's a command, run it.
        if not ctx.command:
            return
        fn = self._commands.get(ctx.command.lower())
        if not fn:
            logger.debug("command_not_found conv_id=%s command=%s", ctx.conv_id, ctx.command)
            self._emit("on_command_not_found", ctx)
            return

        try:
            if _is_coro_fn(fn):
                loop = self._ensure_async_loop()
                logger.debug(
                    "dispatch_command_async conv_id=%s command=%s handler=%s",
                    ctx.conv_id,
                    ctx.command,
                    getattr(fn, "__name__", repr(fn)),
                )
                _safe_create_task(loop, fn(ctx))  # type: ignore[arg-type]
            else:
                logger.debug(
                    "dispatch_command_sync conv_id=%s command=%s handler=%s",
                    ctx.conv_id,
                    ctx.command,
                    getattr(fn, "__name__", repr(fn)),
                )
                fn(ctx)
        except Exception:
            logger.exception("command_handler_failed command=%s handler=%r", ctx.command, fn)
            self._emit("on_command_error", ctx)

    def run(self) -> None:
        if not getattr(self, "_state", None):
            # Allow people to call run() directly without remembering setup().
            self.setup()

        print("Listening for chat.received events...")
        try:
            for item in self._stream_client.activity.stream(stream_config=_stream_config()):
                try:
                    event = as_dict(item)
                    data = event.get("data") or {}
                    if data.get("event_type") != "chat.received":
                        continue

                    payload = data.get("payload") or {}
                    conv_id = payload.get("conversation_id")
                    encoded_event = payload.get("encoded_event")
                    if not conv_id or not encoded_event:
                        logger.debug(
                            "skip_event_missing_fields payload=%s",
                            _summarize_payload(payload),
                        )
                        continue

                    if truthy_env(self._env.get("XCHAT_DEBUG")):
                        logger.debug(
                            "chat_received conv_id=%s payload=%s",
                            conv_id,
                            _summarize_payload(payload),
                        )

                    # Get the encrypted conversation key for this bot.
                    enc_key = payload.get("encrypted_conversation_key")
                    key_version = payload.get("conversation_key_version")
                    if not enc_key:
                        enc_key, key_version = pick_decryptable_key(
                            self._chat, payload.get("conversation_key_change_event")
                        )
                        logger.debug(
                            "picked_conversation_key conv_id=%s ok=%s key_version=%s",
                            conv_id,
                            bool(enc_key),
                            key_version,
                        )
                    if not enc_key or not key_version:
                        logger.warning(
                            "skip_no_decryptable_key conv_id=%s key_version=%s",
                            conv_id,
                            key_version,
                        )
                        continue
                    self._enc_key_cache[str(conv_id)] = str(enc_key)

                    # Decrypt and create context for text messages only.
                    try:
                        decrypted = self._chat.decrypt_event(encoded_event, enc_key)
                    except Exception as e:
                        logger.exception("decrypt_event_failed conv_id=%s", conv_id)
                        self._emit("on_decrypt_error", e, payload)
                        continue

                    message = as_dict(decrypted)
                    text = _get_text_message(message)
                    if text is None:
                        logger.debug(
                            "ignore_non_text_message conv_id=%s type=%s content_type=%s",
                            conv_id,
                            message.get("type"),
                            (message.get("content") or {}).get("content_type"),
                        )
                        continue

                    cmd, args = self._parse_command(text)
                    logger.debug(
                        "parsed_message conv_id=%s command=%s args_count=%d text_len=%d",
                        conv_id,
                        cmd,
                        len(args),
                        len(text),
                    )
                    ctx = Context(
                        bot=self,
                        conv_id=str(conv_id),
                        conv_token=payload.get("conversation_token"),
                        enc_key=self._enc_key_cache[str(conv_id)],
                        key_version=str(key_version),
                        payload=payload,
                        message=message,
                        text=text,
                        command=cmd,
                        args=args,
                    )
                    self._dispatch_context(ctx)
                except Exception:
                    # Don't let one bad event kill the whole stream loop.
                    logger.exception("event_processing_failed")
                    continue
        except StreamError as e:
            logger.error(
                "Activity stream failed: type=%s status=%s message=%s",
                getattr(e, "error_type", None),
                getattr(e, "status_code", None),
                str(e),
            )
            if getattr(e, "response_body", None):
                logger.error("Activity stream response body: %s", e.response_body)
            self._emit("on_error", e)
            raise

