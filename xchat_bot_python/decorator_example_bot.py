from __future__ import annotations

import logging

from .decorators_bot import Context, XChatBot


def main() -> None:
    """
    Example bot implemented using decorators (Discord.py-like style).

    Run with:
      uv run python -m xchat_bot_python.decorator_example_bot
    """

    logger = logging.getLogger("xchat_bot")

    bot = XChatBot(command_prefix="!")

    @bot.event
    def on_message(ctx: Context) -> None:
        # Called for every decrypted Text message (commands and non-commands).
        logger.info("message conv_id=%s text=%r", ctx.conv_id, ctx.text)

    @bot.command("ping")
    async def ping(ctx: Context) -> None:
        await ctx.reply_async("pong")

    @bot.command("echo", aliases=["say"])
    async def echo(ctx: Context) -> None:
        if not ctx.args:
            await ctx.reply_async("usage: !echo <text>")
            return
        await ctx.reply_async(" ".join(ctx.args))

    @bot.event
    def on_command_not_found(ctx: Context) -> None:
        # Optional behavior; comment out if you don't want feedback.
        logger.info("unknown_command conv_id=%s text=%r", ctx.conv_id, ctx.text)

    @bot.event
    def on_error(exc: Exception) -> None:
        # Called when the activity stream raises StreamError (or similar).
        logger.exception("bot_error", exc_info=exc)

    @bot.event
    def on_decrypt_error(exc: Exception, payload: dict) -> None:
        # Called when a specific message fails to decrypt; the bot keeps running.
        logger.exception(
            "decrypt_error conversation_id=%s",
            (payload or {}).get("conversation_id"),
            exc_info=exc,
        )

    bot.run()


if __name__ == "__main__":
    main()

