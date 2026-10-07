import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main as module


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_either_client_ending_stops_sibling(self):
        for failed in ("discord", "telegram"):
            stopped = asyncio.Event()
            async def failure(*args, **kwargs):
                raise RuntimeError("client failed")
            async def companion(*args, **kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            bot = AsyncMock()
            bot.is_closed = lambda: False
            bot.start.side_effect = failure if failed == "discord" else companion
            runner = failure if failed == "telegram" else companion
            config = dict(DISCORD_CHAT_CHANNEL_ID="1", DISCORD_PRIVATE_CHANNEL_ID="2",
                          DISCORD_TOKEN="fake", TELEGRAM_TOKEN="fake", YOUR_TELEGRAM_ID="1")
            with patch.object(module, "load_env_config", return_value=config), \
                 patch.object(module, "DiscordBot", return_value=bot), \
                 patch.object(module, "run_telegram_bot", side_effect=runner):
                main_task = asyncio.create_task(module.main())
                try:
                    await asyncio.wait_for(stopped.wait(), .5)
                    with self.assertRaises(RuntimeError):
                        await main_task
                    bot.close.assert_awaited_once()
                finally:
                    main_task.cancel()
                    await asyncio.gather(main_task, return_exceptions=True)
