"""Offline checks: python -m unittest discover -s tests."""
import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from telegram_bot.bot import DelayedAttachment, DelayedMessage, TelegramBotController
import telegram_bot.bot as telegram_module
import discord_bot.bot as discord_module


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = TelegramBotController.__new__(TelegramBotController)
        c = self.controller
        c.moscow_tz = telegram_module.pytz.timezone("Europe/Moscow")
        c.owner_id = 1
        c.bot_data_dir = Path(self.temp.name)
        c.attachments_dir = c.bot_data_dir / "attachments"
        c.attachments_dir.mkdir()
        c.data_file = c.bot_data_dir / "delayed_messages.json"
        c.delayed_messages = {}
        c.delayed_tasks = {}
        c.next_message_id = 2
        c._delayed_send_lock = asyncio.Lock()
        c.bot = SimpleNamespace(send_message=AsyncMock())
        c.discord_bot = SimpleNamespace(
            _private_channel_id=2,
            send_message_to_channel=AsyncMock(return_value=False),
            send_message_with_files_to_channel=AsyncMock(return_value=False),
            wait_until_ready=AsyncMock(),
        )

    def job(self, with_file=False):
        c = self.controller
        now = datetime.now(c.moscow_tz)
        attachments = []
        if with_file:
            path = c.attachments_dir / "sample.txt"
            path.write_text("sample")
            attachments.append(DelayedAttachment(str(path), "sample.txt", 6))
        job = DelayedMessage(1, "text", now - timedelta(seconds=1), now, attachments)
        c.delayed_messages[1] = job
        return job

    async def test_failed_send_keeps_job_and_files(self):
        c = self.controller
        job = self.job(True)
        await c.schedule_delayed_message(job)
        self.assertIn(1, c.delayed_messages)
        self.assertTrue(Path(job.attachments[0].file_path).exists())
        saved = json.loads(c.data_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["messages"]["1"]["status"], "failed")
        later = DelayedMessage(2, "next", job.date_time, job.created_at)
        self.assertFalse(c._has_same_time_predecessor(later))

    async def test_atomic_save_and_corrupt_load(self):
        c = self.controller
        self.job(True).date_time += timedelta(days=1)
        c.save_delayed_messages()
        original = c.data_file.read_bytes()
        with patch("utils.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                c.save_delayed_messages()
        self.assertEqual(c.data_file.read_bytes(), original)
        c.data_file.write_text("{broken")
        with self.assertRaises(ValueError):
            c.load_delayed_messages()
        self.assertEqual(c.data_file.read_text(), "{broken")

    async def test_creation_save_failure_never_starts_job(self):
        c = self.controller
        future = datetime.now(c.moscow_tz) + timedelta(days=1)
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "delayed_message_id": 2, "delayed_message_text": "draft",
            "delayed_message_datetime": future,
        }), clear=AsyncMock())
        with patch.object(c, "save_delayed_messages", side_effect=OSError("disk failure")):
            self.assertFalse(await c.finalize_delayed_message(state))
        self.assertEqual(c.delayed_messages, {})
        self.assertEqual(c.delayed_tasks, {})
        self.assertEqual(c.next_message_id, 2)
        state.clear.assert_not_awaited()

    async def test_random_time_at_end_of_window(self):
        bot = discord_module.DiscordBot(1, 2)
        for second in (58, 59):
            value = bot.get_random_time_in_range(time(11, 59, second), time(12))
            self.assertGreaterEqual(value, time(11, 59, second))
            self.assertLessEqual(value, time(12))

    async def test_telegram_output_is_bounded_and_unparsed(self):
        c = self.controller
        output = AsyncMock()
        await c.send_text(output, "`[*_" + "😀" * 4096, parse_mode="Markdown")
        sent = output.await_args.args[0]
        self.assertLessEqual(len(sent.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(sent.startswith("`[*_"))
        self.assertIsNone(output.await_args.kwargs.get("parse_mode"))

    async def test_queue_list_has_pages_for_large_queues(self):
        c = self.controller
        job = self.job()
        for i in range(2, 100):
            c.delayed_messages[i] = DelayedMessage(i, "`[*_" * 1024, job.date_time, job.created_at)
        output = AsyncMock()
        callback = SimpleNamespace(data="view_delayed_messages", from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(edit_text=output), answer=AsyncMock())
        await c.view_delayed_messages_callback(callback)
        markup = output.await_args.kwargs["reply_markup"]
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertLessEqual(len(buttons), 34)
        self.assertTrue(any(b.callback_data == "view_delayed_messages_1" for b in buttons))

    async def test_missing_attachment_prevents_any_delivery(self):
        bot = discord_module.DiscordBot(1, 2)
        send = AsyncMock()
        bot.get_channel = lambda _: SimpleNamespace(send=send)
        missing = str(self.controller.attachments_dir / "missing.txt")
        self.assertFalse(await bot.send_message_with_files_to_channel(1, "hello", [missing]))
        send.assert_not_awaited()

    async def test_partial_delivery_retry_skips_confirmed_parts(self):
        bot = discord_module.DiscordBot(1, 2)
        delivered = []
        failed_once = False
        async def send(*args, **kwargs):
            nonlocal failed_once
            text = kwargs.get("content", args[0] if args else "")
            if delivered and not failed_once:
                failed_once = True
                raise ConnectionResetError("second part")
            delivered.append(text)
        channel = SimpleNamespace(send=send)
        bot.get_channel = lambda _: channel
        with patch.object(discord_module.asyncio, "sleep", new=AsyncMock()):
            self.assertTrue(await bot.send_message_to_channel(1, "x" * 2100))
        self.assertEqual(list(map(len, delivered)), [2000, 100])

    async def test_file_progress_survives_manual_retry_and_reload(self):
        c = self.controller
        job = self.job(True)
        job.text = "x" * 2100
        bot = discord_module.DiscordBot(1, 2)
        paths = []
        for index in range(11):
            path = c.attachments_dir / str(index)
            path.write_text("file")
            paths.append(str(path))
        received = []
        async def send(**kwargs):
            if len(received) == 1 and not job.delivery_progress.get("uncertain"):
                raise ConnectionResetError("lost response")
            received.append((len(kwargs.get("files", [])), len(kwargs.get("content") or "")))
        bot.get_channel = lambda _: SimpleNamespace(send=send)
        with patch.object(discord_module.asyncio, "sleep", new=AsyncMock()):
            self.assertFalse(await bot.send_message_with_files_to_channel(1, job.text, paths,
                progress=job.delivery_progress, on_progress=c.save_delayed_messages))
            c.delayed_messages.clear()
            c.load_delayed_messages()
            progress = c.delayed_messages[1].delivery_progress
            self.assertEqual(progress["next_part"], 1)
            self.assertTrue(progress["uncertain"])
            self.assertTrue(await bot.send_message_with_files_to_channel(1, job.text, paths,
                progress=progress, on_progress=c.save_delayed_messages))
        self.assertEqual(received, [(10, 2000), (1, 0), (0, 100)])

    async def test_delete_save_failure_preserves_files_and_task(self):
        c = self.controller
        job = self.job(True)
        task = Mock()
        c.delayed_tasks[1] = task
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1), data="delete_delayed_1",
                                   answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        with patch.object(c, "save_delayed_messages", side_effect=OSError("disk failure")):
            await c.delete_delayed_message_callback(callback)
        self.assertIs(c.delayed_messages[1], job)
        self.assertTrue(Path(job.attachments[0].file_path).exists())
        task.cancel.assert_not_called()

    async def test_duplicate_attachment_names_are_unique(self):
        c = self.controller
        c.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="remote"))
        async def download(remote, path):
            Path(path).write_text(str(c.bot.download_file.await_count))
        c.bot.download_file = AsyncMock(side_effect=download)
        first = await c.download_file("one", "report.txt", 1)
        second = await c.download_file("two", "report.txt", 1)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_text(), "1")
        self.assertEqual(Path(second).read_text(), "2")

    async def test_attachment_save_survives_reload(self):
        c = self.controller
        job = self.job(True)
        job.date_time += timedelta(days=1)
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1), data="save_attachments_1",
                                   answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(return_value={
            "editing_message_id": 1, "new_attachments": list(job.attachments)}))
        job.attachments.clear()
        await c.save_attachments_callback(callback, state)
        c.delayed_messages.clear()
        c.load_delayed_messages()
        self.assertEqual(len(c.delayed_messages[1].attachments), 1)
        state.clear.assert_awaited_once()

    async def test_overdue_jobs_remain_available_without_auto_send(self):
        c = self.controller
        job = self.job(True)
        c.save_delayed_messages()
        c.delayed_messages.clear()
        c.load_delayed_messages()
        self.assertEqual(c.delayed_messages[1].status, "missed")
        self.assertTrue(Path(job.attachments[0].file_path).exists())
        c._restore_delayed_tasks()
        self.assertEqual(c.delayed_tasks, {})

    async def test_channel_cache_miss_fetches_from_discord(self):
        bot = discord_module.DiscordBot(1, 2)
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = lambda _: None
        send = AsyncMock()
        bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(send=send))
        self.assertTrue(await bot.send_message_to_channel(42, "hello"))
        bot.fetch_channel.assert_awaited_once_with(42)
        send.assert_awaited_once()

    async def test_restored_job_waits_for_discord_ready(self):
        c = self.controller
        ready = asyncio.Event()
        c.discord_bot.wait_until_ready = ready.wait
        job = self.job()
        task = asyncio.create_task(c.schedule_delayed_message(job))
        try:
            await asyncio.sleep(0)
            c.discord_bot.send_message_to_channel.assert_not_awaited()
            ready.set()
            await task
            c.discord_bot.send_message_to_channel.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_navigation_discards_draft_without_changing_saved_files(self):
        c = self.controller
        job = self.job(True)
        draft = c.attachments_dir / "draft.txt"
        draft.write_text("draft")
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "new_attachments": [DelayedAttachment(str(draft), "draft.txt", 5)]}), clear=AsyncMock())
        event = SimpleNamespace(data="main_menu", from_user=SimpleNamespace(id=1))
        handler = AsyncMock()
        await c.reset_navigation(handler, event, {"state": state})
        self.assertFalse(draft.exists())
        self.assertTrue(Path(job.attachments[0].file_path).exists())
        state.clear.assert_awaited_once()
        handler.assert_awaited_once()

    async def test_past_explicit_date_and_expired_draft_are_rejected(self):
        c = self.controller
        now = datetime.now(c.moscow_tz)
        past = now - timedelta(days=1)
        with self.assertRaises(ValueError):
            c.parse_datetime_string(past.strftime("%d.%m.%Y %H:%M:%S"))
        self.assertGreater(c.parse_datetime_string(now.strftime("%H:%M")), now)
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "delayed_message_id": 2, "delayed_message_text": "draft",
            "delayed_message_datetime": past,
        }), clear=AsyncMock(), set_state=AsyncMock())
        self.assertFalse(await c.finalize_delayed_message(state))
        self.assertEqual(c.delayed_tasks, {})
        self.assertEqual(c.delayed_messages, {})
        state.clear.assert_not_awaited()
