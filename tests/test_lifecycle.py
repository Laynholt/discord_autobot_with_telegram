import asyncio
import sys
import unittest
import os
import signal
import subprocess
import select
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main as module
from telegram_bot.bot import TelegramBotController
from discord_bot.bot import DiscordBot
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import Update, Message, Chat, User, CallbackQuery
from aiogram.methods import GetMe, GetUpdates
from telegram_bot.bot import BotStates


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dispatcher_back_clears_input_and_polling_stops(self):
        controller = TelegramBotController.__new__(TelegramBotController)
        controller.owner_id = 1
        controller.discord_bot = DiscordBot(1, 2, settings_file=None)
        controller.bot = Bot("123456:offline-test-token")
        original_session = controller.bot.session
        controller.bot.session = AsyncMock()
        async def response(bot, method, **kwargs):
            if isinstance(method, GetMe):
                return User(id=123456, is_bot=True, first_name="test")
            if isinstance(method, GetUpdates):
                await asyncio.Event().wait()
            return True
        controller.bot.session.side_effect = response
        controller.dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        controller._setup_handlers()
        state = controller.dp.fsm.get_context(bot=controller.bot, chat_id=1, user_id=1)
        await state.set_state(BotStates.waiting_message_text)
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"),
                          from_user=User(id=1, is_bot=False, first_name="owner"), text="menu")
        query = CallbackQuery(id="offline", from_user=message.from_user, chat_instance="offline",
                              message=message, data="main_menu")
        try:
            await controller.dp.feed_update(controller.bot, Update(update_id=1, callback_query=query))
            self.assertIsNone(await state.get_state())
            await controller.dp.feed_update(controller.bot, Update(update_id=2, message=message))
            self.assertEqual(controller.discord_bot.chat_channel_message, "+")

            controller.delayed_messages = {}
            controller.delayed_tasks = {}
            controller._polling_task = None
            controller.save_delayed_messages = lambda: None
            listener = asyncio.create_task(controller.start_polling())
            for _ in range(100):
                if controller.dp._running_lock.locked():
                    break
                await asyncio.sleep(.01)
            await controller.stop()
            await listener
            self.assertFalse(controller.dp._running_lock.locked())
            self.assertFalse(controller.dp._handle_update_tasks)
        finally:
            await controller.discord_bot.close()
            await original_session.close()

    async def test_client_cleanup_drains_owned_tasks_and_sessions(self):
        bot = DiscordBot(1, 2, settings_file=None)
        bot._scheduler_task = asyncio.create_task(asyncio.Event().wait())
        scheduler = bot._scheduler_task
        await bot.close()
        self.assertTrue(scheduler.done())
        self.assertIsNone(bot._scheduler_task)

        controller = TelegramBotController.__new__(TelegramBotController)
        controller.save_delayed_messages = lambda: None
        controller.discord_bot = SimpleNamespace(on_auto_mark_failure=None)
        controller.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        stopped = asyncio.Event()
        controller._polling_task = asyncio.create_task(stopped.wait())
        async def stop_polling():
            stopped.set()
        update = asyncio.create_task(asyncio.Event().wait())
        delayed = asyncio.create_task(asyncio.Event().wait())
        controller.dp = SimpleNamespace(stop_polling=stop_polling, _handle_update_tasks={update})
        controller.delayed_tasks = {1: delayed}
        await controller.stop()
        self.assertTrue(update.done())
        self.assertTrue(delayed.done())
        self.assertTrue(controller._polling_task.done())
        controller.bot.session.close.assert_awaited_once()

    async def test_shutdown_drains_jobs_created_by_a_finishing_update(self):
        controller = TelegramBotController.__new__(TelegramBotController)
        controller.save_delayed_messages = lambda: None
        controller.discord_bot = SimpleNamespace(on_auto_mark_failure=None)
        controller.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        controller.delayed_tasks = {}
        controller._polling_task = asyncio.create_task(asyncio.Event().wait())
        async def stop_polling():
            controller.delayed_tasks[1] = asyncio.create_task(asyncio.Event().wait())
            controller._polling_task.cancel()
        controller.dp = SimpleNamespace(stop_polling=stop_polling, _handle_update_tasks=set())
        try:
            await controller.stop()
            self.assertFalse(controller.delayed_tasks)
        finally:
            for task in controller.delayed_tasks.values():
                task.cancel()
            await asyncio.gather(*controller.delayed_tasks.values(), return_exceptions=True)

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

    async def test_terminal_eof_stops_only_an_interactive_process(self):
        loop = asyncio.get_running_loop()
        callbacks = {}
        stop = asyncio.Event()
        stdin = SimpleNamespace(isatty=lambda: True, fileno=lambda: 9)
        with patch.object(module.sys, "stdin", stdin), \
             patch.object(module.os, "name", "posix"), \
             patch.object(module.os, "tcgetpgrp", return_value=1, create=True), \
             patch.object(module.os, "getpgrp", return_value=1, create=True), \
             patch.object(module.os, "read", return_value=b""), \
             patch.object(loop, "add_reader", side_effect=lambda fd, cb: callbacks.update({fd: cb})), \
             patch.object(loop, "remove_reader"):
            with module.termination_handlers(stop):
                callbacks[9]()
                self.assertTrue(stop.is_set())
            callbacks.clear()
            stdin.isatty = lambda: False
            with module.termination_handlers(asyncio.Event()):
                self.assertEqual(callbacks, {})


@unittest.skipUnless(os.name == "posix", "real terminal checks require POSIX")
class PosixLifecycleTests(unittest.TestCase):
    def test_signals_and_ctrl_keys_stop_both_clients(self):
        import pty
        child = str(Path(__file__).with_name("lifecycle_child.py"))
        for trigger in (signal.SIGINT, signal.SIGTERM, signal.SIGTSTP, signal.SIGHUP,
                        b"\x03", b"\x1a", b"\x04"):
            with self.subTest(trigger=trigger):
                pid, fd = pty.fork()
                if pid == 0:
                    os.execv(sys.executable, [sys.executable, child])
                output = b""
                try:
                    for _ in range(100):
                        if select.select([fd], [], [], .1)[0]:
                            output += os.read(fd, 65536)
                        if b"READY" in output:
                            break
                    self.assertIn(b"READY", output)
                    if isinstance(trigger, bytes):
                        os.write(fd, trigger)
                    else:
                        os.kill(pid, trigger)
                    exited = False
                    for _ in range(100):
                        if select.select([fd], [], [], .1)[0]:
                            try:
                                output += os.read(fd, 65536)
                            except OSError:
                                pass
                        result, status = os.waitpid(pid, os.WNOHANG)
                        if result:
                            exited = True
                            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode())
                            break
                    self.assertTrue(exited, output.decode())
                    for marker in (b"DISCORD_STOPPED", b"TELEGRAM_STOPPED", b"DISCORD_CLOSED", b"EXITED"):
                        self.assertIn(marker, output)
                finally:
                    if not locals().get("exited", False):
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    os.close(fd)

    def test_closed_noninteractive_stdin_keeps_service_running(self):
        child = str(Path(__file__).with_name("lifecycle_child.py"))
        process = subprocess.Popen([sys.executable, child], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            output = b""
            while b"READY" not in output:
                self.assertTrue(select.select([process.stdout], [], [], 10)[0])
                output += os.read(process.stdout.fileno(), 65536)
            self.assertIsNone(process.poll())
            process.send_signal(signal.SIGTERM)
            remaining, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, remaining.decode())
            self.assertIn(b"EXITED", remaining)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
