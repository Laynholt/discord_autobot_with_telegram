import os
import pytz
import json
import shutil
import asyncio
import logging
from uuid import uuid4
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import contextmanager
from utils import atomic_write_json

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from discord_bot import DiscordBot

# Настройка логирования
_log = logging.getLogger(__name__)


# Состояния для FSM
class BotStates(StatesGroup):
    waiting_message_text = State()
    waiting_day_number = State()
    waiting_next_auto_mark_time = State()
    waiting_delayed_message_text = State()
    waiting_delayed_message_datetime = State()
    waiting_delayed_message_attachments = State()
    editing_delayed_message_text = State()
    editing_delayed_message_datetime = State()
    editing_delayed_message_attachments = State()
    adding_attachments_to_existing = State()


@dataclass
class DelayedAttachment:
    file_path: str
    original_name: str
    file_size: int
    is_image: bool = False

@dataclass
class DelayedMessage:
    id: int
    text: str
    date_time: datetime
    created_at: datetime
    attachments: List[DelayedAttachment] = field(default_factory=list)
    status: str = "pending"
    delivery_progress: dict = field(default_factory=dict)


class TelegramBotController:
    """
    Telegram бот для управления Discord ботом
    """
    
    def __init__(self, discord_bot: DiscordBot, bot_token: str, owner_id: int):
        self.discord_bot = discord_bot
        self.bot = Bot(token=bot_token)
        self.dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        self.owner_id = owner_id
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        
        # Хранилище отложенных сообщений
        self.delayed_messages: dict[int, DelayedMessage] = {}
        self.next_message_id = 1
        
        # Создаем папку для данных бота
        self.bot_data_dir = Path("bot_data")
        self.bot_data_dir.mkdir(exist_ok=True)
        
        # Создаем подпапку для вложений
        self.attachments_dir = self.bot_data_dir / "attachments"
        self.attachments_dir.mkdir(exist_ok=True)
        
        # Путь к файлу с данными отложенных сообщений
        self.data_file = self.bot_data_dir / "delayed_messages.json"
        self.discord_bot.on_auto_mark_failure = self.notify_auto_mark_failure
        
        # Задачи для отложенных сообщений
        self.delayed_tasks: dict[int, asyncio.Task] = {}
        # Последовательная отправка нужна, чтобы сообщения с одинаковым временем
        # уходили строго в порядке добавления.
        self._delayed_send_lock = asyncio.Lock()
        
        self._setup_handlers()
        
        # Загружаем сохраненные отложенные сообщения при инициализации
        self.load_delayed_messages()
        
        # Восстанавливаем задачи планировщика для загруженных сообщений
        self._restore_delayed_tasks()
    
    def save_delayed_messages(self):
        """Сохраняет отложенные сообщения в JSON файл"""
        try:
            data = {
                "next_message_id": self.next_message_id,
                "messages": {}
            }
            
            # Конвертируем отложенные сообщения в формат для JSON
            for msg_id, delayed_msg in self.delayed_messages.items():
                data["messages"][str(msg_id)] = {
                    "id": delayed_msg.id,
                    "text": delayed_msg.text,
                    "date_time": delayed_msg.date_time.isoformat(),
                    "created_at": delayed_msg.created_at.isoformat(),
                    "status": delayed_msg.status,
                    "delivery_progress": delayed_msg.delivery_progress,
                    "attachments": [
                        {
                            "file_path": att.file_path,
                            "original_name": att.original_name,
                            "file_size": att.file_size,
                            "is_image": att.is_image
                        }
                        for att in delayed_msg.attachments
                    ]
                }
            
            atomic_write_json(self.data_file, data)
                
            _log.info(f"Сохранено {len(self.delayed_messages)} отложенных сообщений в {self.data_file}")
            
        except Exception as e:
            _log.error(f"Ошибка при сохранении отложенных сообщений: {e}")
            raise

    @contextmanager
    def persist_changes(self):
        """Roll back synchronous queue mutations if their disk commit fails."""
        previous = dict(self.delayed_messages)
        fields = {key: {**vars(msg), "attachments": list(msg.attachments)}
                  for key, msg in previous.items()}
        previous_id = self.next_message_id
        try:
            yield
            self.save_delayed_messages()
        except Exception:
            for key, msg in previous.items():
                vars(msg).update(fields[key])
            self.delayed_messages.clear()
            self.delayed_messages.update(previous)
            self.next_message_id = previous_id
            raise
    
    def load_delayed_messages(self):
        """Загружает отложенные сообщения из JSON файла"""
        if not self.data_file.exists():
            _log.info("Файл с отложенными сообщениями не найден, начинаем с пустого состояния")
            return
        
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.next_message_id = data.get("next_message_id", 1)
            messages_data = data.get("messages", {})
            
            current_time = datetime.now(self.moscow_tz)
            
            for msg_id_str, msg_data in messages_data.items():
                msg_id = int(msg_id_str)
                
                # Парсим дату и время
                date_time = datetime.fromisoformat(msg_data["date_time"])
                created_at = datetime.fromisoformat(msg_data["created_at"])
                
                status = msg_data.get("status", "pending")
                if date_time <= current_time and status == "pending":
                    status = "missed"
                
                # Создаем объекты вложений
                attachments = []
                for att_data in msg_data.get("attachments", []):
                    attachment = DelayedAttachment(
                        file_path=att_data["file_path"],
                        original_name=att_data["original_name"],
                        file_size=att_data["file_size"],
                        is_image=att_data.get("is_image", False)
                    )
                    attachments.append(attachment)
                
                # Создаем отложенное сообщение
                delayed_msg = DelayedMessage(
                    id=msg_id,
                    text=msg_data["text"],
                    date_time=date_time,
                    created_at=created_at,
                    attachments=attachments,
                    status=status,
                    delivery_progress=msg_data.get("delivery_progress", {})
                )
                
                self.delayed_messages[msg_id] = delayed_msg
            
            _log.info(f"Загружено {len(self.delayed_messages)} активных отложенных сообщений")
                
        except Exception as e:
            _log.error(f"Ошибка при загрузке отложенных сообщений: {e}")
            raise ValueError(f"Не удалось восстановить очередь из {self.data_file}; файл сохранён") from e
    
    def _restore_delayed_tasks(self):
        """Восстанавливает задачи планировщика для загруженных отложенных сообщений"""
        # Явно сортируем, чтобы после перезапуска сохранить порядок отправки
        # при совпадающем времени (по времени создания/ID).
        sorted_messages = sorted(
            self.delayed_messages.items(),
            key=lambda item: (item[1].date_time, item[1].created_at, item[1].id)
        )
        
        for msg_id, delayed_msg in sorted_messages:
            if delayed_msg.status != "pending":
                continue
            try:
                # Создаем задачу для отправки сообщения
                task = asyncio.create_task(self.schedule_delayed_message(delayed_msg))
                self.delayed_tasks[msg_id] = task
                _log.info(f"Восстановлена задача для отложенного сообщения #{msg_id} на {delayed_msg.date_time}")
            except Exception as e:
                _log.error(f"Ошибка при восстановлении задачи для сообщения #{msg_id}: {e}")
        
        _log.info(f"Восстановлено {len(self.delayed_tasks)} задач планировщика")

    def _has_same_time_predecessor(self, delayed_msg: DelayedMessage) -> bool:
        """
        Проверяет, есть ли еще неотправленное сообщение с тем же временем,
        которое было добавлено раньше текущего.
        """
        current_order_key = (delayed_msg.created_at, delayed_msg.id)
        
        for other in self.delayed_messages.values():
            if other.status != "pending":
                continue
            if other.id == delayed_msg.id:
                continue
            if other.date_time != delayed_msg.date_time:
                continue
            
            other_order_key = (other.created_at, other.id)
            if other_order_key < current_order_key:
                return True
        
        return False
    
    async def validate_file(self, file_id: str) -> tuple[bool, str, int]:
        """
        Валидация файла по размеру (до 10МБ)
        
        Returns:
            tuple: (is_valid, error_message, file_size)
        """
        try:
            file_info = await self.bot.get_file(file_id)
            file_size = file_info.file_size
            
            if file_size is None:
                return False, f"Невалидный размер файла!", 0
            
            # Проверяем размер файла (10МБ = 10 * 1024 * 1024 байт)
            max_size = 10 * 1024 * 1024
            if file_size > max_size:
                size_mb = file_size / (1024 * 1024)
                return False, f"Файл слишком большой: {size_mb:.1f} МБ (максимум 10 МБ)", file_size
            
            return True, "", file_size
            
        except Exception as e:
            return False, f"Ошибка при проверке файла: {e}", 0
    
    async def download_file(self, file_id: str, file_name: str, message_id: int) -> str:
        """
        Скачивание файла во временную папку
        
        Returns:
            str: Путь к сохраненному файлу
        """
        file_info = await self.bot.get_file(file_id)
        
        # Создаем уникальное имя файла
        safe_name = "".join(c for c in file_name if c.isalnum() or c in ".-_")
        temp_filename = f"{message_id}_{uuid4().hex}_{safe_name[:80]}"
        temp_path = self.attachments_dir / temp_filename
        
        # Скачиваем файл
        try:
            await self.bot.download_file(file_info.file_path, temp_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return str(temp_path)
    
    def is_image_file(self, file_name: str) -> bool:
        """Проверка, является ли файл изображением"""
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
        return Path(file_name).suffix.lower() in image_extensions
    
    def escape_markdown(self, text: str) -> str:
        """Экранирование специальных символов для Telegram Markdown"""
        # Символы, которые нужно экранировать в MarkdownV2
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        escaped_text = text
        for char in special_chars:
            escaped_text = escaped_text.replace(char, f'\\{char}')
        return escaped_text
    
    def cleanup_message_files(self, message_id: int, msg: DelayedMessage | None = None):
        """Удаление всех файлов отложенного сообщения"""
        msg = msg or self.delayed_messages.get(message_id)
        if msg is not None:
            for attachment in msg.attachments:
                try:
                    if os.path.exists(attachment.file_path):
                        os.remove(attachment.file_path)
                        _log.info(f"Удален временный файл: {attachment.file_path}")
                except Exception as e:
                    _log.error(f"Ошибка при удалении файла {attachment.file_path}: {e}")
    
    async def cleanup_creating_message_files(self, state: FSMContext):
        """Удаление временных файлов создаваемого сообщения"""
        try:
            data = await state.get_data()
            attachments = data.get("delayed_message_attachments", []) + data.get("new_attachments", [])
            
            for attachment in attachments:
                # DelayedAttachment объекты имеют атрибут file_path
                if hasattr(attachment, 'file_path'):
                    file_path = attachment.file_path
                else:
                    continue
                    
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        _log.info(f"Удален временный файл создаваемого сообщения: {file_path}")
                except Exception as e:
                    _log.error(f"Ошибка при удалении временного файла {file_path}: {e}")
                    
        except Exception as e:
            _log.error(f"Ошибка при очистке файлов создаваемого сообщения: {e}")
    
    def split_long_text(self, text: str, max_length: int = 2000) -> List[str]:
        """Разбивка длинного текста на части"""
        if len(text) <= max_length:
            return [text]
        
        parts = []
        current_part = ""
        
        # Разбиваем по строкам, чтобы сохранить читаемость
        lines = text.split('\n')
        
        for line in lines:
            # Если даже одна строка превышает лимит, принудительно разбиваем
            if len(line) > max_length:
                if current_part:
                    parts.append(current_part.rstrip())
                    current_part = ""
                
                # Разбиваем длинную строку по словам
                words = line.split(' ')
                temp_line = ""
                
                for word in words:
                    if len(temp_line + word + " ") <= max_length:
                        temp_line += word + " "
                    else:
                        if temp_line:
                            parts.append(temp_line.rstrip())
                        temp_line = word + " "
                
                if temp_line:
                    current_part = temp_line
            else:
                # Проверяем, поместится ли строка в текущую часть
                if len(current_part + line + "\n") <= max_length:
                    current_part += line + "\n"
                else:
                    if current_part:
                        parts.append(current_part.rstrip())
                    current_part = line + "\n"
        
        if current_part:
            parts.append(current_part.rstrip())
        
        return parts if parts else [text[:max_length]]
    
    def split_attachments(self, attachments: List[DelayedAttachment], max_per_message: int = 10) -> List[List[DelayedAttachment]]:
        """Разбивка вложений на группы для отправки несколькими сообщениями"""
        if len(attachments) <= max_per_message:
            return [attachments]
        
        groups = []
        for i in range(0, len(attachments), max_per_message):
            groups.append(attachments[i:i + max_per_message])
        
        return groups
    
    def check_owner(self, user_id: int) -> bool:
        """Проверка, что пользователь является владельцем"""
        if user_id != self.owner_id:
            _log.warning(f"Попытка доступа от неавторизованного пользователя: {user_id}")
            return False
        return True

    @staticmethod
    def text_preview(text: str, limit: int = 4096) -> str:
        encoded = text.encode("utf-16-le")
        if len(encoded) <= limit * 2:
            return text
        return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"

    async def send_text(self, method, *args, **kwargs):
        """Bound previews at the Telegram API boundary; user text stays literal."""
        args = list(args)
        index = 1 if len(args) > 1 else 0
        if args:
            args[index] = self.text_preview(args[index])
        kwargs["parse_mode"] = None
        return await method(*args, **kwargs)
    
    def _setup_handlers(self):
        """Настройка обработчиков команд"""
        self.dp.callback_query.outer_middleware(self.reset_navigation)
        self.dp.message.outer_middleware(self.reset_navigation)
        
        # Основные команды
        self.dp.message(Command("start"))(self.start_command)
        self.dp.message(Command("menu"))(self.show_main_menu)
        
        # Callback обработчики
        self.dp.callback_query(F.data == "main_menu")(self.main_menu_callback)
        self.dp.callback_query(F.data == "auto_mark_menu")(self.auto_mark_menu_callback)
        self.dp.callback_query(F.data == "message_settings_menu")(self.message_settings_menu_callback)
        self.dp.callback_query(F.data == "wait_day_menu")(self.wait_day_menu_callback)
        self.dp.callback_query(F.data == "delayed_messages_menu")(self.delayed_messages_menu_callback)
        
        # Автоотметка
        self.dp.callback_query(F.data == "toggle_auto_mark")(self.toggle_auto_mark_callback)
        self.dp.callback_query(F.data == "regenerate_time")(self.regenerate_time_callback)
        self.dp.callback_query(F.data == "set_next_auto_mark_time")(self.set_next_auto_mark_time_callback)
        
        # Настройки сообщения
        self.dp.callback_query(F.data == "set_message_text")(self.set_message_text_callback)
        
        # День ожидания
        self.dp.callback_query(F.data == "set_wait_day")(self.set_wait_day_callback)
        self.dp.callback_query(F.data == "clear_wait_day")(self.clear_wait_day_callback)
        
        # Отложенные сообщения
        self.dp.callback_query(F.data == "create_delayed_message")(self.create_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("view_delayed_messages"))(self.view_delayed_messages_callback)
        self.dp.callback_query(F.data.startswith("edit_delayed_"))(self.edit_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("delete_delayed_"))(self.delete_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("retry_delayed_"))(self.retry_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("edit_text_"))(self.edit_delayed_text_callback)
        self.dp.callback_query(F.data.startswith("edit_datetime_"))(self.edit_delayed_datetime_callback)
        self.dp.callback_query(F.data.startswith("manage_attachments_"))(self.manage_attachments_callback)
        self.dp.callback_query(F.data.startswith("add_attachments_"))(self.add_attachments_callback)
        self.dp.callback_query(F.data.startswith("view_attachment_"))(self.view_attachment_callback)
        self.dp.callback_query(F.data.startswith("delete_attachment_"))(self.delete_attachment_callback)
        self.dp.callback_query(F.data.startswith("save_attachments_"))(self.save_attachments_callback)
        self.dp.callback_query(F.data == "create_without_files")(self.create_without_files_callback)
        self.dp.callback_query(F.data == "cancel_creating_message")(self.cancel_creating_message_callback)
        
        # Обработчики состояний
        self.dp.message(BotStates.waiting_message_text)(self.process_message_text)
        self.dp.message(BotStates.waiting_day_number)(self.process_day_number)
        self.dp.message(BotStates.waiting_next_auto_mark_time)(self.process_next_auto_mark_time)
        self.dp.message(BotStates.waiting_delayed_message_text)(self.process_delayed_message_text)
        self.dp.message(BotStates.waiting_delayed_message_datetime)(self.process_delayed_message_datetime)
        self.dp.message(BotStates.waiting_delayed_message_attachments)(self.process_delayed_message_attachments)
        self.dp.message(BotStates.editing_delayed_message_text)(self.process_edit_delayed_text)
        self.dp.message(BotStates.editing_delayed_message_datetime)(self.process_edit_delayed_datetime)
        self.dp.message(BotStates.editing_delayed_message_attachments)(self.process_edit_delayed_attachments)
        self.dp.message(BotStates.adding_attachments_to_existing)(self.process_adding_attachments)

    async def reset_navigation(self, handler, event, data):
        """Leaving an input flow discards its uncommitted files and FSM state."""
        callback_data = getattr(event, "data", None)
        text = getattr(event, "text", None) or ""
        navigation = callback_data is not None and not (
            callback_data == "create_without_files" or
            callback_data == "cancel_creating_message" or
            callback_data.startswith("save_attachments_")
        )
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        state = data.get("state")
        if state is not None and self.check_owner(event.from_user.id) and (
            navigation or command in {"/start", "/menu"}
        ):
            await self.cleanup_creating_message_files(state)
            await state.clear()
        try:
            return await handler(event, data)
        except (OSError, ValueError) as error:
            _log.exception("Изменения не применены")
            if callback_data is not None:
                await event.answer(self.text_preview(f"❌ Изменения не сохранены: {error}", 200))
            else:
                await self.send_text(event.answer, f"❌ Изменения не сохранены: {error}")
    
    def get_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру главного меню"""
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔔 Ежедневная автоотметка", callback_data="auto_mark_menu"))
        builder.row(InlineKeyboardButton(text="💬 Текст автоотметки", callback_data="message_settings_menu"))
        builder.row(InlineKeyboardButton(text="📅 Отложить автоотметку до дня", callback_data="wait_day_menu"))
        builder.row(InlineKeyboardButton(text="⏰ Отложенные сообщения", callback_data="delayed_messages_menu"))
        return builder.as_markup()
    
    def get_back_keyboard(self, back_to: str = "main_menu") -> InlineKeyboardMarkup:
        """Создает клавиатуру с кнопкой 'Назад'"""
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back_to))
        return builder.as_markup()

    def _get_auto_mark_toggle_button_text(self) -> str:
        """Текст кнопки включения/выключения автоотметки"""
        return (
            "🔴 Выключить"
            if self.discord_bot.should_send_mark_message
            else "🟢 Включить"
        )

    def get_auto_mark_menu_keyboard(self, back_to: str = "main_menu") -> InlineKeyboardMarkup:
        """Клавиатура раздела автоотметки"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=self._get_auto_mark_toggle_button_text(),
                callback_data="toggle_auto_mark"
            )
        )
        builder.row(InlineKeyboardButton(text="🕒 Задать время вручную", callback_data="set_next_auto_mark_time"))
        if self.discord_bot.should_send_mark_message:
            builder.row(InlineKeyboardButton(text="🎲 Перегенерировать время", callback_data="regenerate_time"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back_to))
        return builder.as_markup()
    
    async def start_command(self, message: types.Message):
        """Обработчик команды /start"""
        if not self.check_owner(message.from_user.id):
            return
            
        await self.send_text(message.answer,
            f"👋 Привет! Я бот для управления Discord ботом.\n"
            f"🤖 Ваш ID: {message.from_user.id}\n\n"
            f"Выберите действие из меню ниже:",
            reply_markup=self.get_main_menu_keyboard()
        )
    
    async def show_main_menu(self, message: types.Message):
        """Показать главное меню"""
        if not self.check_owner(message.from_user.id):
            return
            
        await self.send_text(message.answer,
            "🏠 Главное меню управления Discord ботом:",
            reply_markup=self.get_main_menu_keyboard()
        )
    
    async def main_menu_callback(self, callback: types.CallbackQuery):
        """Callback для возврата в главное меню"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await self.send_text(callback.message.edit_text,
            "🏠 Главное меню управления Discord ботом:",
            reply_markup=self.get_main_menu_keyboard()
        )
        await callback.answer()
    
    # === АВТООТМЕТКА ===
    
    async def auto_mark_menu_callback(self, callback: types.CallbackQuery):
        """Меню автоотметки"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        status = "✅ Включена" if self.discord_bot.should_send_mark_message else "❌ Отключена"
        next_send_time = self.discord_bot.next_target_time
        
        await self.send_text(callback.message.edit_text,
            f"🔔 *Ежедневная автоотметка*\n\n"
            f"Автоматическая отправка сообщений в рабочие дни (пн-пт) с 10:30 до 12:00 МСК\n\n"
            f"Текущий статус: _{status}_\n"
            f"Следующая отправка: _{next_send_time}_",
            reply_markup=self.get_auto_mark_menu_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def toggle_auto_mark_callback(self, callback: types.CallbackQuery):
        """Переключение автоотметки"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        if self.discord_bot.should_send_mark_message:
            self.discord_bot.disable_sending_in_chat()
            status = "❌ Отключена"
            action = "отключена"
        else:
            self.discord_bot.enable_sending_in_chat()
            status = "✅ Включена"
            action = "включена"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text=self._get_auto_mark_toggle_button_text(), callback_data="toggle_auto_mark"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="auto_mark_menu"))
        
        await self.send_text(callback.message.edit_text,
            f"🔔 *Автоотметка {action}!*\n\n"
            f"Текущий статус: _{status}_",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer(f"Автоотметка {action}!")
        _log.info(f"Автоотметка {action} пользователем {callback.from_user.id}")

    async def set_next_auto_mark_time_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало ручной установки времени следующей автоотметки"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return

        await self.send_text(callback.message.edit_text,
            "🕒 Введите время следующей автоотметки в формате HH:MM или HH:MM:SS\n"
            "Допустимый диапазон: 10:30:00 - 12:00:00 МСК",
            reply_markup=self.get_back_keyboard("auto_mark_menu")
        )
        await state.set_state(BotStates.waiting_next_auto_mark_time)
        await callback.answer()

    async def process_next_auto_mark_time(self, message: types.Message, state: FSMContext):
        """Обработка ручного времени следующей автоотметки"""
        if not self.check_owner(message.from_user.id):
            return

        raw_value = (message.text or "").strip()
        parsed_time = None

        for time_format in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(raw_value, time_format).time()
                break
            except ValueError:
                continue

        if parsed_time is None:
            await self.send_text(message.answer,
                "❌ Неверный формат времени. Используйте HH:MM или HH:MM:SS:"
            )
            return

        try:
            self.discord_bot.set_next_target_time_once(parsed_time)
        except ValueError as e:
            await self.send_text(message.answer, f"❌ {e}")
            return

        await state.clear()
        await self.send_text(message.answer,
            f"✅ *Следующая автоотметка обновлена!*\n\n"
            f"Одноразово установлено время: `{parsed_time.strftime('%H:%M:%S')}`\n"
            f"Далее время снова будет генерироваться случайно.\n\n"
            f"Следующая отправка: _{self.discord_bot.next_target_time}_",
            reply_markup=self.get_back_keyboard("auto_mark_menu"),
            parse_mode="Markdown"
        )
        _log.info(
            "Следующее время автоотметки вручную установлено пользователем %s: %s",
            message.from_user.id,
            parsed_time.strftime("%H:%M:%S")
        )

    async def regenerate_time_callback(self, callback: types.CallbackQuery):
        """Перегенерация времени отправки"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        # Сохраняем старое время для уведомления
        old_time = self.discord_bot.get_target_time_raw().strftime('%H:%M:%S')
        
        # Перегенерируем время
        self.discord_bot.regenerate_next_target_time()
        
        # Получаем новое время
        status = "✅ Включена" if self.discord_bot.should_send_mark_message else "❌ Отключена"
        next_send_time = self.discord_bot.next_target_time
        
        # Обновляем кнопки
        try:
            await self.send_text(callback.message.edit_text,
                f"🔔 *Ежедневная автоотметка*\n\n"
                f"Автоматическая отправка сообщений в рабочие дни (пн-пт) с 10:30 до 12:00 МСК\n\n"
                f"Текущий статус: _{status}_\n"
                f"Следующая отправка: _{next_send_time}_\n\n"
                f"🎲 *Время перегенерировано!*",
                reply_markup=self.get_auto_mark_menu_keyboard(),
                parse_mode="Markdown"
            )
            await callback.answer("🎲 Время отправки перегенерировано!")
            _log.info(f"Время отправки перегенерировано пользователем {callback.from_user.id}: {old_time} → {next_send_time}")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await callback.answer("🎲 Время перегенерировано, но отображение не изменилось")
                _log.info(f"Время отправки перегенерировано пользователем {callback.from_user.id}: {old_time} → {next_send_time} (сообщение не обновлено)")
            else:
                raise e
    
    # === НАСТРОЙКИ СООБЩЕНИЯ ===
    
    async def message_settings_menu_callback(self, callback: types.CallbackQuery):
        """Меню настроек сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        current_message = self.discord_bot.chat_channel_message
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data="set_message_text"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu"))
        
        await self.send_text(callback.message.edit_text,
            f"💬 *Текст ежедневной автоотметки*\n\n"
            f"Сообщение, которое автоматически отправляется в рабочие дни\n\n"
            f"Текущий текст:\n`{current_message}`",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def set_message_text_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало изменения текста сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await self.send_text(callback.message.edit_text,
            "✏️ Введите новый текст для автоотправки:",
            reply_markup=self.get_back_keyboard("message_settings_menu")
        )
        await state.set_state(BotStates.waiting_message_text)
        await callback.answer()
    
    async def process_message_text(self, message: types.Message, state: FSMContext):
        """Обработка нового текста сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        new_text = (message.text or "").strip()
        self.discord_bot.chat_channel_message = new_text
        
        await state.clear()
        await self.send_text(message.answer,
            f"✅ *Текст сообщения обновлен!*\n\n"
            f"Новый текст:\n{self.text_preview(new_text, 1000)}",
            reply_markup=self.get_back_keyboard("message_settings_menu"),
            parse_mode="Markdown"
        )
        _log.info(f"Текст сообщения изменен на: {new_text}")
    
    # === ДЕНЬ ОЖИДАНИЯ ===
    
    async def wait_day_menu_callback(self, callback: types.CallbackQuery):
        """Меню дня ожидания"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        current_day = self.discord_bot.wait_until_target_day
        day_text = str(current_day) if current_day is not None else "Не установлен"
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📅 Установить день", callback_data="set_wait_day"))
        if current_day:
            builder.row(InlineKeyboardButton(text="🗑 Очистить", callback_data="clear_wait_day"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu"))
        
        await self.send_text(callback.message.edit_text,
            f"📅 *Отложить автоотметку до дня*\n\n"
            f"Приостановить ежедневную автоотметку до указанного числа месяца\n\n"
            f"Текущий день ожидания: _{day_text}_",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def set_wait_day_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало установки дня ожидания"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await self.send_text(callback.message.edit_text,
            "📅 Введите номер дня месяца (1-31):",
            reply_markup=self.get_back_keyboard("wait_day_menu")
        )
        await state.set_state(BotStates.waiting_day_number)
        await callback.answer()
    
    async def process_day_number(self, message: types.Message, state: FSMContext):
        """Обработка номера дня"""
        if not self.check_owner(message.from_user.id):
            return
            
        try:
            day = int((message.text or "").strip())
            if not 1 <= day <= 31:
                await self.send_text(message.answer,
                    "❌ Номер дня должен быть от 1 до 31. Попробуйте еще раз:"
                )
                return
            
            self.discord_bot.wait_until_target_day = day
            await state.clear()
            
            await self.send_text(message.answer,
                f"✅ *Автоотметка отложена!*\n\n"
                f"Ежедневная автоотметка приостановлена до {day} числа.",
                reply_markup=self.get_back_keyboard("wait_day_menu"),
                parse_mode="Markdown"
            )
            _log.info(f"День ожидания установлен на: {day}")
            
        except ValueError:
            await self.send_text(message.answer,
                "❌ Введите корректный номер дня (число от 1 до 31):"
            )
    
    async def clear_wait_day_callback(self, callback: types.CallbackQuery):
        """Очистка дня ожидания"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        self.discord_bot.wait_until_target_day = None
        
        await self.send_text(callback.message.edit_text,
            f"✅ *Автоотметка возобновлена!*\n\n"
            f"Ежедневная автоотметка возобновлена в обычном режиме.",
            reply_markup=self.get_back_keyboard("wait_day_menu"),
            parse_mode="Markdown"
        )
        await callback.answer("Автоотметка возобновлена!")
        _log.info("День ожидания очищен")
    
    # === ОТЛОЖЕННЫЕ СООБЩЕНИЯ ===
    
    async def delayed_messages_menu_callback(self, callback: types.CallbackQuery):
        """Меню отложенных сообщений"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        count = len(self.delayed_messages)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="➕ Создать", callback_data="create_delayed_message"))
        if count > 0:
            builder.row(InlineKeyboardButton(text="📋 Просмотреть все", callback_data="view_delayed_messages"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu"))
        
        await self.send_text(callback.message.edit_text,
            f"⏰ *Отложенные сообщения*\n\n"
            f"Количество активных: {count}",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def create_delayed_message_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало создания отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_creating_message"))
        
        await self.send_text(callback.message.edit_text,
            "✏️ Введите текст отложенного сообщения:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(BotStates.waiting_delayed_message_text)
        await callback.answer()
    
    async def process_delayed_message_text(self, message: types.Message, state: FSMContext):
        """Обработка текста отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        text = (message.text or "").strip()
        await state.update_data(delayed_message_text=text)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_creating_message"))
        
        await self.send_text(message.answer,
            f"📝 Текст сохранен:\n{self.text_preview(text, 1000)}\n\n"
            f"⏰ Теперь введите дату и время отправки.\n\n"
            f"*Форматы:*\n"
            f"• `ЧЧ:ММ` или `ЧЧ:ММ:СС` - только время (сегодня или завтра)\n"
            f"• `ДД.ММ ЧЧ:ММ` или `ДД.ММ ЧЧ:ММ:СС` - дата и время текущего года\n"
            f"• `ДД.ММ.ГГГГ ЧЧ:ММ` или `ДД.ММ.ГГГГ ЧЧ:ММ:СС` - полная дата и время\n\n"
            f"*Примеры:*\n"
            f"• `15:30` или `15:30:45`\n"
            f"• `25.12 18:00` или `25.12 18:00:30`\n"
            f"• `01.01.2025 00:00` или `01.01.2025 00:00:15`",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await state.set_state(BotStates.waiting_delayed_message_datetime)
    
    async def process_delayed_message_datetime(self, message: types.Message, state: FSMContext):
        """Обработка даты и времени отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
        
        datetime_str = (message.text or "").strip()
        
        try:
            target_datetime = self.parse_datetime_string(datetime_str)
            data = await state.get_data()
            text = data["delayed_message_text"]
            
            # Сохраняем данные и переходим к этапу добавления файлов
            await state.update_data(
                delayed_message_text=text,
                delayed_message_datetime=target_datetime,
                delayed_message_id=self.next_message_id,
                delayed_message_attachments=data.get("delayed_message_attachments", [])
            )
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="✅ Создать без файлов", callback_data="create_without_files"))
            builder.row(InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_creating_message"))
            
            await self.send_text(message.answer,
                f"📝 *Текст сохранен:*\n{self.text_preview(text, 1000)}\n\n"
                f"⏰ *Время отправки:* _{target_datetime.strftime('%d.%m.%Y %H:%M:%S')} МСК_\n\n"
                f"📎 *Добавление файлов и изображений*\n\n"
                f"Теперь можете отправить файлы или изображения для отложенного сообщения. "
                f"Максимальный размер каждого файла: 10 МБ.\n\n"
                f"Когда закончите добавлять файлы, нажмите '✅ Создать сообщение'",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            await state.set_state(BotStates.waiting_delayed_message_attachments)
            
        except ValueError as e:
            await self.send_text(message.answer, f"❌ Ошибка в формате даты/времени: {e}\n\nПопробуйте еще раз:")
    
    def parse_datetime_string(self, datetime_str: str) -> datetime:
        """Парсинг строки даты и времени"""
        moscow_now = datetime.now(self.moscow_tz)
        
        # Только время (ЧЧ:ММ или ЧЧ:ММ:СС)
        if ":" in datetime_str and "." not in datetime_str:
            try:
                # Пробуем сначала с секундами
                if datetime_str.count(":") == 2:
                    time_obj = datetime.strptime(datetime_str, "%H:%M:%S").time()
                else:
                    time_obj = datetime.strptime(datetime_str, "%H:%M").time()
                
                target_dt = moscow_now.replace(
                    hour=time_obj.hour,
                    minute=time_obj.minute,
                    second=time_obj.second,
                    microsecond=0
                )
                # Если время уже прошло сегодня, берем завтра
                if target_dt <= moscow_now:
                    target_dt += timedelta(days=1)
                return target_dt
            except ValueError:
                raise ValueError("Неверный формат времени. Используйте ЧЧ:ММ или ЧЧ:ММ:СС")
        
        # Дата и время без года (ДД.ММ ЧЧ:ММ или ДД.ММ ЧЧ:ММ:СС)
        elif datetime_str.count(".") == 1:
            try:
                # Определяем количество двоеточий для выбора формата времени
                if datetime_str.count(":") == 2:
                    dt = datetime.strptime(f"{datetime_str}.{moscow_now.year}", "%d.%m %H:%M:%S.%Y")
                else:
                    dt = datetime.strptime(f"{datetime_str}.{moscow_now.year}", "%d.%m %H:%M.%Y")
                
                target_dt = moscow_now.replace(
                    year=dt.year,
                    month=dt.month,
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    second=dt.second,
                    microsecond=0
                )
                # Если дата в прошлом, берем следующий год
                if target_dt <= moscow_now:
                    target_dt = target_dt.replace(year=moscow_now.year + 1)
                return target_dt
            except ValueError:
                raise ValueError("Неверный формат даты. Используйте ДД.ММ ЧЧ:ММ или ДД.ММ ЧЧ:ММ:СС")
        
        # Полная дата и время (ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ:СС)
        elif datetime_str.count(".") == 2:
            try:
                # Определяем количество двоеточий для выбора формата времени
                if datetime_str.count(":") == 2:
                    dt = datetime.strptime(datetime_str, "%d.%m.%Y %H:%M:%S")
                else:
                    dt = datetime.strptime(datetime_str, "%d.%m.%Y %H:%M")
                    
                target_dt = moscow_now.replace(
                    year=dt.year,
                    month=dt.month,
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    second=dt.second,
                    microsecond=0
                )
            except ValueError:
                raise ValueError("Неверный формат полной даты. Используйте ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ:СС")
            if target_dt <= moscow_now:
                raise ValueError("Время отправки должно быть в будущем")
            return target_dt
        
        else:
            raise ValueError("Неизвестный формат даты/времени")
    
    async def schedule_delayed_message(self, delayed_msg: DelayedMessage):
        """Планировщик отложенного сообщения"""
        try:
            moscow_now = datetime.now(self.moscow_tz)
            wait_seconds = (delayed_msg.date_time - moscow_now).total_seconds()
            
            if wait_seconds > 0:
                _log.info(f"Ожидание отправки сообщения #{delayed_msg.id} в течение {wait_seconds} секунд")
                await asyncio.sleep(wait_seconds)

            await self.discord_bot.wait_until_ready()

            while True:
                async with self._delayed_send_lock:
                    # Сообщение могли отменить, пока задача ждала своего времени
                    if delayed_msg.id not in self.delayed_messages:
                        _log.info(f"Отложенное сообщение #{delayed_msg.id} уже отсутствует в хранилище, отправка пропущена")
                        return

                    if self._has_same_time_predecessor(delayed_msg):
                        # Есть более раннее сообщение с тем же временем — ждём своей очереди.
                        pass
                    else:
                        # Отправляем сообщение (с файлами или без)
                        if delayed_msg.attachments:
                            file_paths = [att.file_path for att in delayed_msg.attachments]
                            success = await self.discord_bot.send_message_with_files_to_channel(
                                channel_id=self.discord_bot._private_channel_id,
                                message_content=delayed_msg.text,
                                file_paths=file_paths,
                                progress=delayed_msg.delivery_progress,
                                on_progress=self.save_delayed_messages
                            )
                        else:
                            success = await self.discord_bot.send_message_to_channel(
                                channel_id=self.discord_bot._private_channel_id,
                                message_content=delayed_msg.text,
                                progress=delayed_msg.delivery_progress,
                                on_progress=self.save_delayed_messages
                            )
                        
                        if success:
                            _log.info(f"Отложенное сообщение #{delayed_msg.id} успешно отправлено")
                            # Уведомляем в телеграм
                            try:
                                await self.send_text(self.bot.send_message,
                                    self.owner_id,
                                    f"✅ *Отложенное сообщение отправлено!*\n\n"
                                    f"📝 Текст:\n{self.text_preview(delayed_msg.text, 1000)}\n"
                                    f"⏰ Время: _{delayed_msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК_",
                                    parse_mode="Markdown"
                                )
                            except Exception as e:
                                _log.error(f"Не удалось отправить уведомление о доставке: {e}")
                        else:
                            _log.error(f"Не удалось отправить отложенное сообщение #{delayed_msg.id}")
                            # Уведомляем об ошибке
                            try:
                                await self.send_text(self.bot.send_message,
                                    self.owner_id,
                                    f"❌ *Ошибка отправки отложенного сообщения!*\n\n"
                                    f"📝 Текст:\n{self.text_preview(delayed_msg.text, 1000)}\n"
                                    f"⏰ Время: _{delayed_msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК_",
                                    parse_mode="Markdown"
                                )
                            except Exception as e:
                                _log.error(f"Не удалось отправить уведомление об ошибке: {e}")
                        
                        try:
                            with self.persist_changes():
                                if success:
                                    del self.delayed_messages[delayed_msg.id]
                                else:
                                    delayed_msg.status = "failed"
                        except OSError:
                            # Keep a delivered job inert in memory until a later save succeeds.
                            delayed_msg.status = "sent" if success else "failed"
                            raise
                        if success:
                            self.cleanup_message_files(delayed_msg.id, delayed_msg)
                        if delayed_msg.id in self.delayed_tasks:
                            del self.delayed_tasks[delayed_msg.id]

                        return

                await asyncio.sleep(0.2)
                
        except asyncio.CancelledError:
            _log.info(f"Отправка отложенного сообщения #{delayed_msg.id} отменена")
        except Exception as e:
            _log.exception(f"Ошибка при отправке отложенного сообщения #{delayed_msg.id}: {e}")
    
    async def view_delayed_messages_callback(self, callback: types.CallbackQuery):
        """Просмотр всех отложенных сообщений"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        if not self.delayed_messages:
            await self.send_text(callback.message.edit_text,
                "📋 Отложенных сообщений нет.",
                reply_markup=self.get_back_keyboard("delayed_messages_menu")
            )
            await callback.answer()
            return
        
        # Сортируем по времени отправки
        sorted_messages = sorted(self.delayed_messages.values(), key=lambda x: x.date_time)
        page = int(callback.data.rsplit("_", 1)[-1]) if callback.data.rsplit("_", 1)[-1].isdigit() else 0
        page = min(max(page, 0), (len(sorted_messages) - 1) // 15)
        page_messages = sorted_messages[page * 15:(page + 1) * 15]
        
        text = "📋 *Отложенные сообщения:*\n\n"
        
        builder = InlineKeyboardBuilder()
        for msg in page_messages:
            # Ограничиваем длину текста для отображения
            preview_text = msg.text[:30] + "..." if len(msg.text) > 30 else msg.text
            attachments_info = ""
            if msg.attachments:
                attachments_count = len(msg.attachments)
                images_count = sum(1 for att in msg.attachments if att.is_image)
                files_count = attachments_count - images_count
                
                if images_count and files_count:
                    attachments_info = f" 📁{files_count} 🖼{images_count}"
                elif images_count:
                    attachments_info = f" 🖼{images_count}"
                elif files_count:
                    attachments_info = f" 📁{files_count}"
            
            text += f"*№{msg.id}* — _{msg.date_time.strftime('%d.%m %H:%M')}_{attachments_info} [{msg.status}]\n`{preview_text}`\n\n"
            
            # Добавляем кнопки управления
            builder.row(
                InlineKeyboardButton(text=f"✏️ #{msg.id}", callback_data=f"edit_delayed_{msg.id}"),
                InlineKeyboardButton(text=f"🗑 #{msg.id}", callback_data=f"delete_delayed_{msg.id}")
            )
        
        if page:
            builder.row(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"view_delayed_messages_{page - 1}"))
        if (page + 1) * 15 < len(sorted_messages):
            builder.row(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"view_delayed_messages_{page + 1}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="delayed_messages_menu"))
        
        await self.send_text(callback.message.edit_text,
            text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def edit_delayed_message_callback(self, callback: types.CallbackQuery):
        """Меню редактирования отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        msg = self.delayed_messages[message_id]
        
        builder = InlineKeyboardBuilder()
        if msg.status in {"failed", "missed"}:
            builder.row(InlineKeyboardButton(text="🔄 Повторить отправку", callback_data=f"retry_delayed_{message_id}"))
        builder.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit_text_{message_id}"))
        builder.row(InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_datetime_{message_id}"))
        builder.row(InlineKeyboardButton(text="📎 Управление файлами", callback_data=f"manage_attachments_{message_id}"))
        builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_delayed_{message_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="view_delayed_messages"))
        
        # Формируем информацию о вложениях
        attachments_info = ""
        if msg.delivery_progress.get("uncertain"):
            attachments_info += "\n⚠️ Ответ Discord потерян. Проверьте последнюю часть перед повтором: она могла доставиться."
        if msg.attachments:
            attachments_info += f"\n*Вложения:* {len(msg.attachments)}"
            for att in msg.attachments[:3]:  # Показываем первые 3 файла
                att_type = "🖼" if att.is_image else "📁"
                size_mb = att.file_size / (1024 * 1024)
                attachments_info += f"\n{att_type} {self.text_preview(att.original_name, 200)} ({size_mb:.2f} МБ)"
            if len(msg.attachments) > 3:
                attachments_info += f"\n... и еще {len(msg.attachments) - 3}"
        
        await self.send_text(callback.message.edit_text,
            f"📝 *Редактирование сообщения #{message_id}*\n\n"
            f"*Текст:*\n{self.text_preview(msg.text, 1000)}\n"
            f"*Время отправки:* _{msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК_\n"
            f"*Создано:* _{msg.created_at.strftime('%d.%m.%Y %H:%M:%S')} МСК_{attachments_info}",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()

    async def retry_delayed_message_callback(self, callback: types.CallbackQuery):
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        message_id = int(callback.data.split("_")[-1])
        job = self.delayed_messages.get(message_id)
        if job is None or job.status not in {"failed", "missed"}:
            await callback.answer("❌ Сообщение уже отправляется или отсутствует")
            return
        try:
            with self.persist_changes():
                job.status = "pending"
                job.date_time = datetime.now(self.moscow_tz)
        except OSError:
            await callback.answer("❌ Не удалось сохранить. Повторите позже.")
            return
        self.delayed_tasks[message_id] = asyncio.create_task(self.schedule_delayed_message(job))
        await callback.answer("🔄 Повторная отправка запущена")
    
    async def edit_delayed_text_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало редактирования текста отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        await state.update_data(editing_message_id=message_id)
        msg = self.delayed_messages[message_id]
        
        await self.send_text(callback.message.edit_text,
            f"✏️ *Редактирование текста сообщения #{message_id}*\n\n"
            f"Текущий текст:\n{self.text_preview(msg.text, 1000)}\n\n"
            f"Введите новый текст:",
            reply_markup=self.get_back_keyboard("view_delayed_messages"),
            parse_mode="Markdown"
        )
        await state.set_state(BotStates.editing_delayed_message_text)
        await callback.answer()
    
    async def process_edit_delayed_text(self, message: types.Message, state: FSMContext):
        """Обработка нового текста отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        data = await state.get_data()
        message_id = data["editing_message_id"]
        new_text = (message.text or "").strip()
        
        if message_id not in self.delayed_messages:
            await self.send_text(message.answer, "❌ Сообщение не найдено")
            await state.clear()
            return

        if self.delayed_messages[message_id].delivery_progress.get("next_part", 0):
            await self.send_text(message.answer, "❌ Сообщение частично отправлено. Создайте новое для изменения текста.")
            return
        
        try:
            with self.persist_changes():
                self.delayed_messages[message_id].text = new_text
        except OSError:
            await self.send_text(message.answer, "❌ Не удалось сохранить. Повторите позже.")
            return
        
        await state.clear()
        
        await self.send_text(message.answer,
            f"✅ *Текст сообщения #{message_id} обновлен!*\n\n"
            f"Новый текст:\n{self.text_preview(new_text, 1000)}",
            reply_markup=self.get_back_keyboard("view_delayed_messages"),
            parse_mode="Markdown"
        )
        _log.info(f"Текст отложенного сообщения #{message_id} изменен")
    
    async def edit_delayed_datetime_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало редактирования времени отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        await state.update_data(editing_message_id=message_id)
        msg = self.delayed_messages[message_id]
        
        await self.send_text(callback.message.edit_text,
            f"⏰ *Редактирование времени сообщения #{message_id}*\n\n"
            f"Текущее время: _{msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК_\n\n"
            f"Введите новое время отправки:\n\n"
            f"*Форматы:*\n"
            f"• `ЧЧ:ММ` или `ЧЧ:ММ:СС` - только время\n"
            f"• `ДД.ММ ЧЧ:ММ` или `ДД.ММ ЧЧ:ММ:СС` - дата и время\n"
            f"• `ДД.ММ.ГГГГ ЧЧ:ММ` или `ДД.ММ.ГГГГ ЧЧ:ММ:СС` - полная дата и время",
            reply_markup=self.get_back_keyboard("view_delayed_messages"),
            parse_mode="Markdown"
        )
        await state.set_state(BotStates.editing_delayed_message_datetime)
        await callback.answer()
    
    async def process_edit_delayed_datetime(self, message: types.Message, state: FSMContext):
        """Обработка нового времени отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        data = await state.get_data()
        message_id = data["editing_message_id"]
        datetime_str = (message.text or "").strip()
        
        if message_id not in self.delayed_messages:
            await self.send_text(message.answer, "❌ Сообщение не найдено")
            await state.clear()
            return
        
        try:
            new_datetime = self.parse_datetime_string(datetime_str)

            with self.persist_changes():
                self.delayed_messages[message_id].date_time = new_datetime
                self.delayed_messages[message_id].status = "pending"
            
            # Отменяем старую задачу
            if message_id in self.delayed_tasks:
                self.delayed_tasks[message_id].cancel()
                del self.delayed_tasks[message_id]
            
            # Создаем новую задачу
            delayed_msg = self.delayed_messages[message_id]
            task = asyncio.create_task(self.schedule_delayed_message(delayed_msg))
            self.delayed_tasks[message_id] = task
            
            await state.clear()
            
            await self.send_text(message.answer,
                f"✅ *Время сообщения #{message_id} обновлено!*\n\n"
                f"Новое время: _{new_datetime.strftime('%d.%m.%Y %H:%M:%S')} МСК_",
                reply_markup=self.get_back_keyboard("view_delayed_messages"),
                parse_mode="Markdown"
            )
            _log.info(f"Время отложенного сообщения #{message_id} изменено на {new_datetime}")
            
        except ValueError as e:
            await self.send_text(message.answer, f"❌ Ошибка в формате даты/времени: {e}\n\nПопробуйте еще раз:")
        except OSError:
            await self.send_text(message.answer, "❌ Не удалось сохранить. Повторите позже.")
    
    async def delete_delayed_message_callback(self, callback: types.CallbackQuery):
        """Удаление отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        msg = self.delayed_messages[message_id]
        try:
            with self.persist_changes():
                del self.delayed_messages[message_id]
        except OSError:
            await callback.answer("❌ Не удалось сохранить. Повторите позже.")
            return

        # Отменяем задачу
        if message_id in self.delayed_tasks:
            self.delayed_tasks[message_id].cancel()
            del self.delayed_tasks[message_id]
        
        # Очищаем временные файлы и удаляем сообщение
        self.cleanup_message_files(message_id, msg)
        
        await self.send_text(callback.message.edit_text,
            f"✅ *Отложенное сообщение #{message_id} удалено!*\n\n"
            f"Текст удаленного сообщения:\n{self.text_preview(msg.text, 1000)}",
            reply_markup=self.get_back_keyboard("view_delayed_messages"),
            parse_mode="Markdown"
        )
        await callback.answer("Сообщение удалено!")
        _log.info(f"Отложенное сообщение #{message_id} удалено")
    
    async def create_without_files_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Создание отложенного сообщения без файлов"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        created = await self.finalize_delayed_message(state)
        await callback.answer("Сообщение создано!" if created else "❌ Сообщение не создано")
    
    async def cancel_creating_message_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Отмена создания отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        # Очищаем временные файлы
        await self.cleanup_creating_message_files(state)
        
        # Очищаем состояние FSM
        await state.clear()
        
        # Возвращаемся в меню отложенных сообщений
        await self.send_text(callback.message.edit_text,
            '❌ Создание сообщения отменено\n\nВсе временные данные и файлы удалены.',
            reply_markup=self.get_back_keyboard("delayed_messages_menu"),
            parse_mode="Markdown"
        )
        
        await callback.answer("Создание отменено")
        _log.info(f"Пользователь {callback.from_user.id} отменил создание отложенного сообщения")
    
    async def manage_attachments_callback(self, callback: types.CallbackQuery):
        """Меню управления вложениями отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1].split(":")[0])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        await self._update_attachments_display(callback, message_id)
        await callback.answer()

    async def view_attachment_callback(self, callback: types.CallbackQuery):
        """Просмотр вложения из меню управления файлами"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return

        parts = callback.data.split("_")
        message_id = int(parts[2])
        attachment_index = int(parts[3])

        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return

        msg = self.delayed_messages[message_id]
        if attachment_index >= len(msg.attachments):
            await callback.answer("❌ Вложение не найдено")
            return

        attachment = msg.attachments[attachment_index]
        if not os.path.exists(attachment.file_path):
            await callback.answer("❌ Файл не найден на диске")
            return

        file_to_send = FSInputFile(attachment.file_path)
        await callback.answer("Открываю файл...")

        try:
            if attachment.is_image:
                await callback.message.answer_photo(
                    photo=file_to_send,
                    caption=self.text_preview(f"🖼 {attachment.original_name}", 1024)
                )
            else:
                await callback.message.answer_document(
                    document=file_to_send,
                    caption=self.text_preview(f"📁 {attachment.original_name}", 1024)
                )
        except Exception as e:
            _log.error(f"Ошибка при отправке вложения '{attachment.original_name}': {e}")
            await callback.message.answer("❌ Не удалось отправить файл")
    
    async def delete_attachment_callback(self, callback: types.CallbackQuery):
        """Удаление конкретного вложения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        parts = callback.data.split("_")
        message_id = int(parts[2])
        attachment_index = int(parts[3])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        msg = self.delayed_messages[message_id]
        if msg.delivery_progress.get("next_part", 0):
            await callback.answer("❌ Нельзя менять файлы частично отправленного сообщения")
            return
        
        if attachment_index >= len(msg.attachments):
            await callback.answer("❌ Вложение не найдено")
            return
        
        try:
            with self.persist_changes():
                deleted_attachment = msg.attachments.pop(attachment_index)
        except OSError:
            await callback.answer("❌ Не удалось сохранить. Повторите позже.")
            return

        # Удаляем файл с диска
        attachment = deleted_attachment
        try:
            if os.path.exists(attachment.file_path):
                os.remove(attachment.file_path)
                _log.info(f"Удален файл вложения: {attachment.file_path}")
        except Exception as e:
            _log.error(f"Ошибка при удалении файла {attachment.file_path}: {e}")
        
        # Обновляем отображение
        await self._update_attachments_display(callback, message_id)
        
        await callback.answer(self.text_preview(f"✅ Вложение '{deleted_attachment.original_name}' удалено", 200))
        _log.info(f"Удалено вложение '{deleted_attachment.original_name}' из сообщения #{message_id}")
    
    async def _update_attachments_display(self, callback: types.CallbackQuery, message_id: int):
        """Вспомогательный метод для обновления отображения вложений без callback.answer"""
        msg = self.delayed_messages[message_id]
        
        page = int(callback.data.split(":")[-1]) if ":" in callback.data else 0
        page = min(max(page, 0), max(0, (len(msg.attachments) - 1) // 10))
        attachments = msg.attachments[page * 10:(page + 1) * 10]
        # Формируем текст с информацией о вложениях
        if msg.attachments:
            text = f"📎 *Управление вложениями сообщения #{message_id}*\n\n"
            text += f"*Всего вложений:* {len(msg.attachments)}\n\n"
            text += "_Нажмите на название файла для просмотра, 🗑 для удаления._\n\n"
            
            for i, att in enumerate(attachments, page * 10 + 1):
                att_type = "🖼" if att.is_image else "📁"
                size_mb = att.file_size / (1024 * 1024)
                text += f"{i}. {att_type} {self.text_preview(att.original_name, 200)}\n"
                text += f"   Размер: {size_mb:.2f} МБ\n\n"
        else:
            text = f"📎 *Управление вложениями сообщения #{message_id}*\n\n"
            text += "У этого сообщения пока нет вложений."
        
        # Создаем кнопки управления
        builder = InlineKeyboardBuilder()
        
        # Если есть вложения, добавляем кнопки просмотра и удаления
        if msg.attachments:
            for i, att in enumerate(attachments, page * 10):
                att_type = "🖼" if att.is_image else "📁"
                short_name = att.original_name[:20] + "..." if len(att.original_name) > 20 else att.original_name
                builder.row(
                    InlineKeyboardButton(
                        text=f"{att_type} {short_name}",
                        callback_data=f"view_attachment_{message_id}_{i}"
                    ),
                    InlineKeyboardButton(
                        text="🗑",
                        callback_data=f"delete_attachment_{message_id}_{i}"
                    )
                )
        
        if page:
            builder.row(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"manage_attachments_{message_id}:{page - 1}"))
        if (page + 1) * 10 < len(msg.attachments):
            builder.row(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"manage_attachments_{message_id}:{page + 1}"))
        # Всегда добавляем кнопку добавления файлов
        builder.row(InlineKeyboardButton(text="➕ Добавить файлы", callback_data=f"add_attachments_{message_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"edit_delayed_{message_id}"))
        
        await self.send_text(callback.message.edit_text,
            text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    
    async def add_attachments_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало добавления новых вложений к существующему сообщению"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[2])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return

        if self.delayed_messages[message_id].delivery_progress.get("next_part", 0):
            await callback.answer("❌ Нельзя менять файлы частично отправленного сообщения")
            return
        
        # Сохраняем ID сообщения для добавления вложений
        await state.update_data(editing_message_id=message_id, new_attachments=[])
        await state.set_state(BotStates.adding_attachments_to_existing)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data=f"manage_attachments_{message_id}"))
        
        await self.send_text(callback.message.edit_text,
            "📎 Добавление новых вложений\n\nОтправьте файлы, изображения, видео или аудио которые хотите добавить к сообщению.\n\nКогда закончите добавлять файлы, нажмите '💾 Сохранить изменения'.",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def process_adding_attachments(self, message: types.Message, state: FSMContext):
        """Обработка добавления новых вложений к существующему сообщению"""
        if not self.check_owner(message.from_user.id):
            return
        
        # Обрабатываем файлы и изображения
        file_info = None
        file_name = None
        
        if message.document:
            file_info = message.document
            file_name = file_info.file_name or "document"
        elif message.photo:
            file_info = message.photo[-1]  # Берем самое большое разрешение
            file_name = f"photo_{file_info.file_id}.jpg"
        elif message.video:
            file_info = message.video
            file_name = file_info.file_name or f"video_{file_info.file_id}.mp4"
        elif message.audio:
            file_info = message.audio
            file_name = file_info.file_name or f"audio_{file_info.file_id}.mp3"
        elif message.voice:
            file_info = message.voice
            file_name = f"voice_{file_info.file_id}.ogg"
        elif message.video_note:
            file_info = message.video_note
            file_name = f"video_note_{file_info.file_id}.mp4"
        else:
            await self.send_text(message.answer, "❌ Поддерживаются только файлы, изображения, видео и аудио.")
            return
        
        # Валидация файла
        is_valid, error_msg, file_size = await self.validate_file(file_info.file_id)
        
        if not is_valid:
            await self.send_text(message.answer, f"❌ {error_msg}")
            return
        
        try:
            data = await state.get_data()
            message_id = data.get("editing_message_id")
            
            if not message_id or message_id not in self.delayed_messages:
                await self.send_text(message.answer, "❌ Сообщение не найдено")
                return
            
            # Скачиваем файл
            file_path = await self.download_file(file_info.file_id, file_name, message_id)
            
            # Создаем вложение
            attachment = DelayedAttachment(
                file_path=file_path,
                original_name=file_name,
                file_size=file_size,
                is_image=self.is_image_file(file_name)
            )
            
            # Keep uploads in the FSM until explicit Save.
            delayed_msg = self.delayed_messages[message_id]
            draft = data.get("new_attachments", []) + [attachment]
            await state.update_data(new_attachments=draft)
            
            # Информируем пользователя
            file_type = "🖼 Изображение" if attachment.is_image else "📁 Файл"
            size_mb = file_size / (1024 * 1024)
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="💾 Сохранить изменения", callback_data=f"save_attachments_{message_id}"))
            builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data=f"manage_attachments_{message_id}"))
            
            await self.send_text(message.answer,
                f"✅ {file_type} добавлен к сообщению!\n\n"
                f"📂 Файл: {self.text_preview(file_name, 200)}\n"
                f"📏 Размер: {size_mb:.2f} МБ\n"
                f"📊 Всего файлов: {len(delayed_msg.attachments) + len(draft)}\n\n"
                f"Можете добавить еще файлы или сохранить изменения.",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            
            _log.info(f"Добавлено вложение '{file_name}' к сообщению #{message_id}")
            
        except Exception as e:
            _log.error(f"Ошибка при добавлении файла к существующему сообщению: {e}")
            await self.send_text(message.answer, f"❌ Ошибка при добавлении файла: {e}")
    
    async def save_attachments_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Сохранение изменений вложений"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[2])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return

        data = await state.get_data()
        if data.get("editing_message_id") != message_id:
            await callback.answer("❌ Это меню устарело. Откройте добавление файлов снова.")
            return
        job = self.delayed_messages[message_id]
        if job.delivery_progress.get("next_part", 0):
            await callback.answer("❌ Нельзя менять файлы частично отправленного сообщения")
            return

        try:
            with self.persist_changes():
                job.attachments.extend(data.get("new_attachments", []))
        except OSError:
            await callback.answer("❌ Не удалось сохранить. Повторите позже.")
            return
        
        await state.clear()
        
        # Возвращаемся к управлению вложениями с обновленным списком
        await self._update_attachments_display(callback, message_id)
        await callback.answer("✅ Изменения сохранены")
    
    async def process_delayed_message_attachments(self, message: types.Message, state: FSMContext):
        """Обработка файлов и изображений для отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
        
        # Обрабатываем файлы и изображения
        file_info = None
        file_name = None
        
        if message.document:
            file_info = message.document
            file_name = file_info.file_name or "document"
        elif message.photo:
            file_info = message.photo[-1]  # Берем самое большое разрешение
            file_name = f"photo_{file_info.file_id}.jpg"
        elif message.video:
            file_info = message.video
            file_name = file_info.file_name or f"video_{file_info.file_id}.mp4"
        elif message.audio:
            file_info = message.audio
            file_name = file_info.file_name or f"audio_{file_info.file_id}.mp3"
        elif message.voice:
            file_info = message.voice
            file_name = f"voice_{file_info.file_id}.ogg"
        elif message.video_note:
            file_info = message.video_note
            file_name = f"video_note_{file_info.file_id}.mp4"
        else:
            await self.send_text(message.answer, "❌ Поддерживаются только файлы, изображения, видео и аудио.")
            return
        
        # Валидация файла
        is_valid, error_msg, file_size = await self.validate_file(file_info.file_id)
        
        if not is_valid:
            await self.send_text(message.answer, f"❌ {error_msg}")
            return
        
        try:
            data = await state.get_data()
            message_id = data["delayed_message_id"]
            attachments = data.get("delayed_message_attachments", [])
            
            # Скачиваем файл
            file_path = await self.download_file(file_info.file_id, file_name, message_id)
            
            # Создаем вложение
            attachment = DelayedAttachment(
                file_path=file_path,
                original_name=file_name,
                file_size=file_size,
                is_image=self.is_image_file(file_name)
            )
            
            attachments.append(attachment)
            await state.update_data(delayed_message_attachments=attachments)
            
            # Информируем пользователя
            file_type = "🖼 Изображение" if attachment.is_image else "📁 Файл"
            size_mb = file_size / (1024 * 1024)
            
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="✅ Создать сообщение", callback_data="create_without_files"))
            builder.row(InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_creating_message"))
            
            await self.send_text(message.answer,
                f"✅ {file_type} добавлен!\n\n"
                f"📂 Файл: {self.text_preview(file_name, 200)}\n"
                f"📏 Размер: {size_mb:.2f} МБ\n"
                f"📊 Всего файлов: {len(attachments)}\n\n"
                f"Можете добавить еще файлы или создать сообщение.",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            
        except Exception as e:
            _log.error(f"Ошибка при добавлении файла: {e}")
            await self.send_text(message.answer, f"❌ Ошибка при добавлении файла: {e}")
    
    async def finalize_delayed_message(self, state: FSMContext):
        """Финализация создания отложенного сообщения"""
        try:
            data = await state.get_data()
            message_id = data["delayed_message_id"]
            text = data["delayed_message_text"]
            target_datetime = data["delayed_message_datetime"]
            attachments = data.get("delayed_message_attachments", [])
            if target_datetime <= datetime.now(self.moscow_tz):
                await state.set_state(BotStates.waiting_delayed_message_datetime)
                raise ValueError("Время отправки уже прошло. Введите новую дату; загруженные файлы сохранены.")
            
            # Создаем отложенное сообщение
            delayed_msg = DelayedMessage(
                id=message_id,
                text=text,
                date_time=target_datetime,
                created_at=datetime.now(self.moscow_tz),
                attachments=attachments
            )
            
            with self.persist_changes():
                self.delayed_messages[message_id] = delayed_msg
                self.next_message_id += 1
            
            # Запускаем задачу отправки
            task = asyncio.create_task(self.schedule_delayed_message(delayed_msg))
            self.delayed_tasks[message_id] = task
            
            await state.clear()
            
            # Формируем сообщение об успешном создании
            attachment_info = ""
            if attachments:
                attachment_info = f"\n📎 Вложений: {len(attachments)}"
                for att in attachments[:3]:  # Показываем первые 3 файла
                    att_type = "🖼" if att.is_image else "📁"
                    attachment_info += f"\n{att_type} {att.original_name}"
                if len(attachments) > 3:
                    attachment_info += f"\n... и еще {len(attachments) - 3}"
            
            # Отправляем в тот же чат где была команда
            await self.send_text(self.bot.send_message,
                self.owner_id,
                f"✅ *Отложенное сообщение создано!*\n\n"
                f"📝 Текст:\n{self.text_preview(text, 1000)}\n"
                f"⏰ Время отправки: _{target_datetime.strftime('%d.%m.%Y %H:%M:%S')} МСК_{attachment_info}",
                reply_markup=self.get_back_keyboard("delayed_messages_menu"),
                parse_mode="Markdown"
            )
            
            _log.info(f"Создано отложенное сообщение #{message_id} на {target_datetime} с {len(attachments)} вложениями")
            return True
            
        except Exception as e:
            _log.error(f"Ошибка при финализации отложенного сообщения: {e}")
            await self.send_text(self.bot.send_message,
                self.owner_id,
                f"❌ Ошибка при создании отложенного сообщения: {e}"
            )
            return False
    
    async def process_edit_delayed_attachments(self, message: types.Message, state: FSMContext):
        """Обработка редактирования вложений отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
        
        await self.send_text(message.answer, "🚧 Редактирование вложений пока не реализовано")
        await state.clear()
    
    async def start_polling(self):
        """Запуск бота"""
        _log.info("Запуск Telegram бота...")
        recovery_count = sum(job.status in {"failed", "missed"} for job in self.delayed_messages.values())
        if recovery_count:
            try:
                await self.send_text(self.bot.send_message, self.owner_id,
                    f"⚠️ Сохранено сообщений, требующих внимания: {recovery_count}. "
                    "Откройте отложенные сообщения для повтора, переноса или удаления.")
            except Exception:
                _log.exception("Не удалось уведомить о восстановленных сообщениях")
        await self.dp.start_polling(self.bot)

    async def notify_auto_mark_failure(self, text: str):
        await self.send_text(self.bot.send_message, self.owner_id, text)
    
    async def stop(self):
        """Остановка бота"""
        # Сохраняем отложенные сообщения перед остановкой
        _log.info("Сохранение отложенных сообщений перед остановкой...")
        try:
            self.save_delayed_messages()
        except OSError:
            _log.exception("Не удалось сохранить очередь при остановке")
        
        # Отменяем все отложенные задачи
        for task in self.delayed_tasks.values():
            task.cancel()
        
        # Останавливаем диспетчер
        try:
            await self.dp.stop_polling()
        except Exception as e:
            _log.error(f"Ошибка при остановке диспетчера: {e}")
        
        # Закрываем сессию бота
        try:
            await self.bot.session.close()
        except Exception as e:
            _log.error(f"Ошибка при закрытии сессии: {e}")
            
        _log.info("Telegram бот остановлен")


async def run_telegram_bot(bot_token: str, owner_id: int | str, discord_bot: DiscordBot):
    """
    Функция для запуска Telegram бота вместе с Discord ботом
    
    Args:
        bot_token: Токен Telegram бота
        owner_id: Ваш Telegram id
        discord_bot: Экземпляр Discord бота для управления
    """
    telegram_bot = None
    try:
        # Создаем и запускаем Telegram бота
        telegram_bot = TelegramBotController(
            discord_bot=discord_bot,
            bot_token=bot_token,
            owner_id=int(owner_id)
        )
        
        _log.info("Telegram бот готов к работе")
        await telegram_bot.start_polling()
        
    except asyncio.CancelledError:
        _log.info("Telegram бот получил сигнал остановки")
        raise  # Передаем CancelledError дальше
    except Exception as e:
        _log.exception(f"Критическая ошибка в Telegram боте: {e}")
        raise
    finally:
        # Корректно останавливаем бота
        if telegram_bot:
            try:
                await telegram_bot.stop()
            except Exception as e:
                _log.error(f"Ошибка при остановке Telegram бота: {e}")
