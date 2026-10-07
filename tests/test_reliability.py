"""Offline checks: python -m unittest discover -s tests."""
import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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
        state = SimpleNamespace(clear=AsyncMock())
        await c.save_attachments_callback(callback, state)
        c.delayed_messages.clear()
        c.load_delayed_messages()
        self.assertEqual(len(c.delayed_messages[1].attachments), 1)
        state.clear.assert_awaited_once()
