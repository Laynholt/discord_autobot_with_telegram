"""Run with: uv run python tests/test_scheduler.py (no Discord connection)."""

import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discord_bot.bot as module


class Clock(datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        return cls.current


async def check_scheduler():
    with patch.object(module, "datetime", Clock):
        tz = module.pytz.timezone("Europe/Moscow")
        Clock.current = tz.localize(datetime(2026, 9, 30, 11, 41, 7))
        bot = module.DiscordBot(chat_channel_id=1, private_channel_id=2, settings_file=None)
        bot.set_next_target_time_once(time(11))
        bot.wait_until_target_day = 5
        delivered = asyncio.Event()

        async def deliver(channel_id, message_content, **kwargs):
            assert (channel_id, message_content) == (1, "+")
            delivered.set()
            return True

        bot.send_message_to_channel = deliver
        scheduler = asyncio.create_task(bot.message_scheduler())
        try:
            await asyncio.sleep(0)
            assert bot.next_target_time == "11:00:00 - 05.10.2026", bot.next_target_time
            assert bot.wait_until_target_day == 5
            bot.wait_until_target_day = None
            assert await asyncio.wait_for(delivered.wait(), timeout=1)
            assert bot.next_target_time.endswith("01.10.2026"), bot.next_target_time
        finally:
            scheduler.cancel()
            await scheduler

        # The date set in September must still be October 5 after waking at 10:30.
        Clock.current = tz.localize(datetime(2026, 9, 30, 11, 41, 7))
        bot = module.DiscordBot(chat_channel_id=1, private_channel_id=2, settings_file=None)
        bot.set_next_target_time_once(time(11))
        bot.wait_until_target_day = 5
        Clock.current = tz.localize(datetime(2026, 10, 5, 10, 30, 0, 1000))
        sent = []

        async def send(channel_id, message_content, **kwargs):
            sent.append((channel_id, message_content))
            return True

        async def advance(target):
            if target.tzinfo is None:
                target = tz.localize(target)
            assert target.date() <= datetime(2026, 10, 6).date(), target
            if sent:
                raise asyncio.CancelledError
            Clock.current = max(Clock.current, target) + timedelta(milliseconds=1)
            return True

        with patch.object(bot, "send_message_to_channel", send), patch.object(bot, "wait_until_next_date", advance):
            await bot.message_scheduler()
            assert sent == [(1, "+")], sent
            assert bot.wait_until_target_day is None
            # Waking after a setting change must not send the same day's mark twice.
            await bot.message_scheduler()
            assert sent == [(1, "+")], sent

        # Clearing, toggling and changing the time interrupt an active wait.
        for change in (
            lambda: setattr(bot, "wait_until_target_day", None),
            bot.disable_sending_in_chat,
            bot.enable_sending_in_chat,
            lambda: bot.set_next_target_time_once(time(11, 30)),
            bot.regenerate_next_target_time,
        ):
            bot._schedule_changed.clear()
            waiter = asyncio.create_task(bot.wait_until_next_date(Clock.current + timedelta(days=1)))
            await asyncio.sleep(0)
            change()
            assert await asyncio.wait_for(waiter, timeout=1) is False

        bot._schedule_changed.clear()
        assert await bot.wait_until_next_date(Clock.current - timedelta(seconds=1)) is True

        # Changing settings during today's wait must cancel that send, then resume safely.
        Clock.current = tz.localize(datetime(2026, 9, 30, 10, 45))
        bot = module.DiscordBot(chat_channel_id=1, private_channel_id=2, settings_file=None)
        bot.set_next_target_time_once(time(11))
        waits = asyncio.Queue()
        original_wait = bot.wait_until_next_date
        sent.clear()

        async def observe_wait(target):
            waits.put_nowait(target)
            return await original_wait(target)

        async def next_wait():
            return await asyncio.wait_for(waits.get(), timeout=1)

        with patch.object(bot, "send_message_to_channel", send), patch.object(bot, "wait_until_next_date", observe_wait):
            scheduler = asyncio.create_task(bot.message_scheduler())
            try:
                assert (await next_wait()).isoformat() == "2026-09-30T11:00:00+03:00"
                for _ in range(2):
                    bot.wait_until_target_day = 5
                    assert (await next_wait()).isoformat() == "2026-10-05T10:30:00+03:00"
                    assert not sent, sent
                bot.wait_until_target_day = None
                assert (await next_wait()).isoformat() == "2026-09-30T11:00:00+03:00"
                bot.set_next_target_time_once(time(10, 45))
                assert (await next_wait()).isoformat() == "2026-10-01T10:30:00+03:00"
                assert sent == [(1, "+")], sent
                for toggle in (bot.disable_sending_in_chat, bot.enable_sending_in_chat):
                    toggle()
                    assert (await next_wait()).isoformat() == "2026-10-01T10:30:00+03:00"
                    assert sent == [(1, "+")], sent
            finally:
                scheduler.cancel()
                await scheduler

        # A day absent from the next month must not crash; weekends move to Monday.
        for now, day, expected in (
            (datetime(2026, 1, 31, 13), 31, "11:00:00 - 02.02.2026"),
            (datetime(2026, 2, 1, 9), 31, "11:00:00 - 31.03.2026"),
            (datetime(2026, 12, 31, 13), 1, "11:00:00 - 01.01.2027"),
            (datetime(2026, 10, 5, 10, 30, 0, 1000), 5, "11:00:00 - 05.10.2026"),
        ):
            Clock.current = tz.localize(now)
            bot = module.DiscordBot(chat_channel_id=1, private_channel_id=2, settings_file=None)
            bot.set_next_target_time_once(time(11))
            bot.wait_until_target_day = day
            assert bot.next_target_time == expected, (now, bot.next_target_time)
            bot.disable_sending_in_chat()
            assert bot.next_target_time == "Отключено"


if __name__ == "__main__":
    asyncio.run(check_scheduler())
    print("Scheduler regression checks passed")
