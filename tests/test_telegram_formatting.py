"""Telegram formatting stays explicit; message text and filenames stay literal."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.formatting import Bold, Code, Text
from telegram_bot.bot import DelayedAttachment, DelayedMessage, TelegramBotController


class TelegramFormattingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = TelegramBotController.__new__(TelegramBotController)
        self.controller.owner_id = 1
        self.controller.delayed_messages = {}
        self.controller.discord_bot = SimpleNamespace(
            should_send_mark_message=True, next_target_time="11:16:32 - 08.10.2026",
            chat_channel_message="hello", get_auto_mark_toggle_button_text=lambda: "Выключить",
        )
        self.output = AsyncMock()
        self.callback = SimpleNamespace(from_user=SimpleNamespace(id=1), data="auto_mark_menu",
            message=SimpleNamespace(edit_text=self.output), answer=AsyncMock())

    def rendered_entities(self):
        sent = self.output.await_args
        text = sent.args[-1]
        self.assertIsNone(sent.kwargs["parse_mode"])
        encoded = text.encode("utf-16-le")
        result = []
        for entity in sent.kwargs["entities"]:
            self.assertGreater(entity.length, 0)
            self.assertLessEqual((entity.offset + entity.length) * 2, len(encoded))
            part = encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")
            result.append((entity.type, part))
        return text, result

    async def test_auto_mark_menu_has_bold_heading_and_italic_values(self):
        await self.controller.auto_mark_menu_callback(self.callback)
        text, entities = self.rendered_entities()
        self.assertNotIn("*", text)
        self.assertNotIn("_", text)
        self.assertIn(("bold", "Ежедневная автоотметка"), entities)
        self.assertIn(("italic", "✅ Включена"), entities)
        self.assertIn(("italic", "11:16:32 - 08.10.2026"), entities)

    async def test_settings_preserve_user_markup_and_clip_long_emoji_text(self):
        value = "`[*_ <b>literal</b> & " + "😀" * 4096
        self.controller.discord_bot.chat_channel_message = value
        await self.controller.message_settings_menu_callback(self.callback)
        text, entities = self.rendered_entities()
        self.assertIn("`[*_ <b>literal</b> & ", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertIn(("bold", "Текст ежедневной автоотметки"), entities)
        self.assertTrue(any(kind == "code" and part.startswith("`[*_") for kind, part in entities))

    async def test_formatted_output_clips_entities_at_utf16_boundary(self):
        await self.controller.send_text(self.output, 1,
            Text("😀 ", Bold("😀" * 4096), Code("unseen")))
        text, entities = self.rendered_entities()
        self.assertEqual(self.output.await_args.args[0], 1)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertEqual(entities, [("bold", "😀" * 2046)])

    async def test_attachment_and_message_views_keep_user_symbols(self):
        name = "report_`[*<b>&.txt"
        now = datetime.now()
        self.controller.delayed_messages[1] = DelayedMessage(
            1, "body_`[*<b>&", now, now, [DelayedAttachment("unused", name, 10)])
        self.callback.data = "edit_delayed_1"
        await self.controller.edit_delayed_message_callback(self.callback)
        text, entities = self.rendered_entities()
        self.assertIn(name, text)
        self.assertIn("body_`[*<b>&", text)
        self.assertIn(("bold", "Вложения:"), entities)
        self.assertNotIn("*Вложения:*", text)
        await self.controller._update_attachments_display(self.callback, 1)
        text, entities = self.rendered_entities()
        self.assertIn(name, text)
        self.assertIn(("bold", "Управление вложениями сообщения #1"), entities)
        self.assertTrue(any(kind == "italic" for kind, _ in entities))

    async def test_queue_previews_keep_markers_inside_code_entity(self):
        now = datetime.now()
        self.controller.delayed_messages[1] = DelayedMessage(1, "body_`[*<b>&", now, now)
        self.callback.data = "view_delayed_messages"
        await self.controller.view_delayed_messages_callback(self.callback)
        text, entities = self.rendered_entities()
        self.assertIn(("bold", "Отложенные сообщения:"), entities)
        self.assertIn(("bold", "№1"), entities)
        self.assertIn(("code", "body_`[*<b>&"), entities)

    async def test_plain_notification_preserves_user_markup(self):
        value = "`[*_ <b>literal</b> &"
        await self.controller.send_text(self.output, value)
        self.assertEqual(self.output.await_args.args[0], value)
        self.assertIsNone(self.output.await_args.kwargs["parse_mode"])
        self.assertFalse(self.output.await_args.kwargs.get("entities"))

    async def test_aiogram_request_uses_entities_without_default_parse_mode(self):
        bot = Bot("123456:offline-test-token", default=DefaultBotProperties(parse_mode="MarkdownV2"))
        bot.session = AsyncMock(return_value=True)
        await self.controller.send_text(bot.send_message, 1,
            Text("😀 ", Bold("Title"), "\n", Code("`[*_ <b>literal</b> &")))
        request = bot.session.await_args.args[1]
        self.assertEqual(request.text, "😀 Title\n`[*_ <b>literal</b> &")
        self.assertIsNone(request.parse_mode)
        self.assertEqual([(e.type, e.offset, e.length) for e in request.entities],
            [("bold", 3, 5), ("code", 9, 21)])
