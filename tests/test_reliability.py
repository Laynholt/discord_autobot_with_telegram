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
