from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from urllib.parse import urlencode
from urllib.request import urlopen

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

    @bot.command("comic")
    async def comic(ctx: Context) -> None:
        if not ctx.args:
            await ctx.reply_async("usage: !comic <comic-name>")
            return
        comic_name = "-".join(ctx.args).strip().lower()
        today = date.today().strftime("%Y/%m/%d")
        url = f"https://www.gocomics.com/{comic_name}/{today}"
        await ctx.reply_async(url)

    @bot.command("stock")
    async def stock(ctx: Context) -> None:
        if not ctx.args:
            await ctx.reply_async("usage: !stock <ticker>")
            return
        api_key = 'VVOJXAHIQKSQESM7'
        if not api_key:
            await ctx.reply_async("stock: missing ALPHAVANTAGE_API_KEY")
            return

        symbol = ctx.args[0].strip().upper()

        def _fetch_quote() -> str:
            params = urlencode(
                {
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol,
                    "apikey": api_key,
                }
            )
            url = f"https://www.alphavantage.co/query?{params}"
            with urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            quote = data.get("Global Quote") or {}
            price = quote.get("05. price")
            change = quote.get("09. change")
            change_pct = quote.get("10. change percent")
            if not price:
                return f"stock: no data for {symbol}"
            return f"{symbol} price={price} change={change} ({change_pct})"

        reply = await asyncio.to_thread(_fetch_quote)
        await ctx.reply_async(reply)

    @bot.event
    def on_command_not_found(ctx: Context) -> None:
        # Optional behavior; comment out if you don't want feedback.
        logger.info("unknown_command conv_id=%s text=%r", ctx.conv_id, ctx.text)

    bot.run()


if __name__ == "__main__":
    main()

