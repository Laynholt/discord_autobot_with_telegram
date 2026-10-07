"""Subprocess boundary for real POSIX signal / terminal tests; no network."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main


class Discord:
    def __init__(self, **kwargs):
        pass

    async def start(self, token):
        try:
            await asyncio.Event().wait()
        finally:
            print("DISCORD_STOPPED", flush=True)

    async def close(self):
        print("DISCORD_CLOSED", flush=True)


async def telegram(*args):
    print("READY", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print("TELEGRAM_STOPPED", flush=True)


config = dict(DISCORD_CHAT_CHANNEL_ID="1", DISCORD_PRIVATE_CHANNEL_ID="2",
              DISCORD_TOKEN="fake", TELEGRAM_TOKEN="fake", YOUR_TELEGRAM_ID="1")
with patch.object(main, "load_env_config", return_value=config), \
     patch.object(main, "DiscordBot", Discord), \
     patch.object(main, "run_telegram_bot", telegram):
    asyncio.run(main.main())
print("EXITED", flush=True)
