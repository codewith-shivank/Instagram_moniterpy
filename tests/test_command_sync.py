import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

import bot


def test_setup_hook_syncs_commands_before_gateway(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr(bot, "sync_commands", sync)
    monitor_bot = bot.MonitorBot(command_prefix="!", intents=discord.Intents.none())

    asyncio.run(monitor_bot.setup_hook())

    sync.assert_awaited_once_with()


def test_sync_commands_replaces_guild_command_set(monkeypatch):
    synced = [SimpleNamespace(name="ban"), SimpleNamespace(name="status")]

    monkeypatch.setattr(bot, "GUILD_ID", "123")
    bot.bot.tree.clear_commands = Mock()
    bot.bot.tree.copy_global_to = Mock()
    bot.bot.tree.sync = AsyncMock(return_value=synced)

    asyncio.run(bot.sync_commands())

    bot.bot.tree.clear_commands.assert_called_once()
    assert bot.bot.tree.clear_commands.call_args.kwargs["guild"].id == 123
    bot.bot.tree.copy_global_to.assert_called_once()
    assert bot.bot.tree.copy_global_to.call_args.kwargs["guild"].id == 123
    bot.bot.tree.sync.assert_awaited_once()
    assert bot.bot.tree.sync.await_args.kwargs["guild"].id == 123


def test_sync_commands_retries_transient_failures(monkeypatch):
    synced = [SimpleNamespace(name="status")]
    attempts = 0

    async def sync(*, guild):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary DNS failure")
        return synced

    monkeypatch.setattr(bot, "GUILD_ID", "456")
    bot.bot.tree.clear_commands = Mock()
    bot.bot.tree.copy_global_to = Mock()
    bot.bot.tree.sync = sync
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())

    asyncio.run(bot.sync_commands())

    assert attempts == 2
    bot.asyncio.sleep.assert_awaited_once()


def test_sync_commands_does_not_retry_non_transient_errors(monkeypatch):
    failure = ValueError("invalid local command definition")

    async def sync(*, guild):
        raise failure

    monkeypatch.setattr(bot, "GUILD_ID", "789")
    bot.bot.tree.clear_commands = Mock()
    bot.bot.tree.copy_global_to = Mock()
    bot.bot.tree.sync = sync
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)

    try:
        asyncio.run(bot.sync_commands())
    except ValueError as error:
        assert error is failure
    else:
        raise AssertionError("sync_commands should propagate non-transient errors")
    sleep.assert_not_awaited()
