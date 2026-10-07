import asyncio
import os
import sys
import signal
from contextlib import contextmanager

from utils import load_env_config
from discord_bot import DiscordBot
from telegram_bot import run_telegram_bot
from custom_logger import setup_logging

_log = setup_logging()


@contextmanager
def termination_handlers(stop: asyncio.Event):
    loop = asyncio.get_running_loop()
    installed = []
    stdin_fd = None

    def request_stop(reason):
        if not stop.is_set():
            _log.info("Получен запрос остановки: %s", reason)
            stop.set()

    def stdin_ready():
        try:
            if not os.read(stdin_fd, 4096):
                loop.remove_reader(stdin_fd)
                request_stop("EOF (Ctrl+D)")
        except OSError:
            loop.remove_reader(stdin_fd)
            request_stop("терминал закрыт")

    try:
        for name in ("SIGINT", "SIGTERM", "SIGTSTP", "SIGQUIT", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, request_stop, name)
                installed.append((sig, previous, True))
            except NotImplementedError:
                signal.signal(sig, lambda number, frame: loop.call_soon_threadsafe(
                    request_stop, signal.Signals(number).name))
                installed.append((sig, previous, False))
        if os.name == "posix" and sys.stdin.isatty():
            fd = sys.stdin.fileno()
            if os.tcgetpgrp(fd) == os.getpgrp():
                loop.add_reader(fd, stdin_ready)
                stdin_fd = fd
        yield
    finally:
        if stdin_fd is not None:
            loop.remove_reader(stdin_fd)
        for sig, previous, async_handler in installed:
            if async_handler:
                loop.remove_signal_handler(sig)
            signal.signal(sig, previous)


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
    stop = asyncio.Event()
    stop_task = asyncio.create_task(stop.wait(), name="termination")
    with termination_handlers(stop):
        try:
            _log.info("Оба бота запущены. Ctrl+C / Ctrl+Z / Ctrl+D завершают процесс")
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if not stop.is_set():
                for task in done:
                    task.result()
                raise RuntimeError("Один из клиентов завершился; остановка обоих ботов")
        finally:
            for task in [*tasks, stop_task]:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, stop_task, return_exceptions=True), timeout=10)
            finally:
                await asyncio.wait_for(discord_bot.close(), timeout=5)
            _log.info("Все боты остановлены")


if __name__ == "__main__":
    asyncio.run(main())
