import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot


@pytest.fixture
def interaction():
    response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    return SimpleNamespace(response=response, followup=followup)


def test_defer_interaction_acknowledges_before_slow_work(interaction):
    asyncio.run(bot.defer_interaction(interaction, ephemeral=True))

    interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)


def test_defer_interaction_does_not_acknowledge_twice(interaction):
    interaction.response.is_done.return_value = True

    asyncio.run(bot.defer_interaction(interaction))

    interaction.response.defer.assert_not_awaited()


def test_reply_uses_followup_after_defer(interaction):
    interaction.response.is_done.return_value = True

    asyncio.run(bot.reply(interaction, "done", ephemeral=True))

    interaction.followup.send.assert_awaited_once_with("done", ephemeral=True)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_message.assert_not_awaited()


def test_invalid_username_responds_without_defer(monkeypatch):
    response = SimpleNamespace(
        is_done=Mock(return_value=False),
        defer=AsyncMock(),
        send_message=AsyncMock(),
    )
    interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(bot, "clean_username", lambda value: "")

    asyncio.run(bot.ban.callback(interaction, "not valid"))

    response.send_message.assert_awaited_once()
    response.defer.assert_not_awaited()


def test_is_transient_discord_error_classifies_dns_and_server_failures():
    import aiohttp
    import discord

    assert bot.is_transient_discord_error(OSError("DNS lookup failed"))
    assert bot.is_transient_discord_error(aiohttp.ClientError("connection reset"))
    assert bot.is_transient_discord_error(discord.GatewayNotFound())
    assert bot.is_transient_discord_error(Mock(spec=discord.HTTPException, status=503))


def test_is_transient_discord_error_does_not_retry_invalid_token():
    import discord

    assert not bot.is_transient_discord_error(discord.LoginFailure("invalid token"))


def test_run_bot_retries_transient_disconnect(monkeypatch):
    starts = 0
    sleeps = []

    async def start(*args, **kwargs):
        nonlocal starts
        starts += 1
        if starts == 1:
            raise OSError("temporary DNS failure")
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.bot, "start", start)
    monkeypatch.setattr(bot.bot, "is_closed", lambda: True)

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run_bot())

    assert starts == 2
    assert sleeps == [bot.RECONNECT_MIN_SECONDS]
