import asyncio

from utils import load_env_config
from discord_bot import DiscordBot
from telegram_bot import run_telegram_bot
from custom_logger import setup_logging

_log = setup_logging()


async def main():
    config = load_env_config()
    discord_bot = DiscordBot(
        chat_channel_id=int(config["DISCORD_CHAT_CHANNEL_ID"]),
        private_channel_id=int(config["DISCORD_PRIVATE_CHANNEL_ID"]),
    )
    tasks = [
        asyncio.create_task(discord_bot.start(config["DISCORD_TOKEN"]), name="discord_bot"),
        asyncio.create_task(run_telegram_bot(config["TELEGRAM_TOKEN"],
                                             config["YOUR_TELEGRAM_ID"], discord_bot),
                            name="telegram_bot"),
    ]
    try:
        _log.info("Оба бота запущены. Для остановки нажмите Ctrl+C")
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("Один из клиентов завершился; остановка обоих ботов")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        finally:
            await discord_bot.close()
        _log.info("Все боты остановлены")


if __name__ == "__main__":
    asyncio.run(main())
