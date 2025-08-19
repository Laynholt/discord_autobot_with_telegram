import pytz
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from discord_bot import DiscordBot

# Настройка логирования
_log = logging.getLogger(__name__)


# Состояния для FSM
class BotStates(StatesGroup):
    waiting_message_text = State()
    waiting_day_number = State()
    waiting_delayed_message_text = State()
    waiting_delayed_message_datetime = State()
    editing_delayed_message_text = State()
    editing_delayed_message_datetime = State()


@dataclass
class DelayedMessage:
    id: int
    text: str
    date_time: datetime
    created_at: datetime


class TelegramBotController:
    """
    Telegram бот для управления Discord ботом
    """
    
    def __init__(self, discord_bot: DiscordBot, bot_token: str, owner_id: int):
        self.discord_bot = discord_bot
        self.bot = Bot(token=bot_token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.owner_id = owner_id
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        
        # Хранилище отложенных сообщений
        self.delayed_messages: dict[int, DelayedMessage] = {}
        self.next_message_id = 1
        
        # Задачи для отложенных сообщений
        self.delayed_tasks: dict[int, asyncio.Task] = {}
        
        self._setup_handlers()
    
    def check_owner(self, user_id: int) -> bool:
        """Проверка, что пользователь является владельцем"""
        if user_id != self.owner_id:
            _log.warning(f"Попытка доступа от неавторизованного пользователя: {user_id}")
            return False
        return True
    
    def _setup_handlers(self):
        """Настройка обработчиков команд"""
        
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
        
        # Настройки сообщения
        self.dp.callback_query(F.data == "set_message_text")(self.set_message_text_callback)
        
        # День ожидания
        self.dp.callback_query(F.data == "set_wait_day")(self.set_wait_day_callback)
        self.dp.callback_query(F.data == "clear_wait_day")(self.clear_wait_day_callback)
        
        # Отложенные сообщения
        self.dp.callback_query(F.data == "create_delayed_message")(self.create_delayed_message_callback)
        self.dp.callback_query(F.data == "view_delayed_messages")(self.view_delayed_messages_callback)
        self.dp.callback_query(F.data.startswith("edit_delayed_"))(self.edit_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("delete_delayed_"))(self.delete_delayed_message_callback)
        self.dp.callback_query(F.data.startswith("edit_text_"))(self.edit_delayed_text_callback)
        self.dp.callback_query(F.data.startswith("edit_datetime_"))(self.edit_delayed_datetime_callback)
        
        # Обработчики состояний
        self.dp.message(BotStates.waiting_message_text)(self.process_message_text)
        self.dp.message(BotStates.waiting_day_number)(self.process_day_number)
        self.dp.message(BotStates.waiting_delayed_message_text)(self.process_delayed_message_text)
        self.dp.message(BotStates.waiting_delayed_message_datetime)(self.process_delayed_message_datetime)
        self.dp.message(BotStates.editing_delayed_message_text)(self.process_edit_delayed_text)
        self.dp.message(BotStates.editing_delayed_message_datetime)(self.process_edit_delayed_datetime)
    
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
    
    async def start_command(self, message: types.Message):
        """Обработчик команды /start"""
        if not self.check_owner(message.from_user.id):
            return
            
        await message.answer(
            f"👋 Привет! Я бот для управления Discord ботом.\n"
            f"🤖 Ваш ID: {message.from_user.id}\n\n"
            f"Выберите действие из меню ниже:",
            reply_markup=self.get_main_menu_keyboard()
        )
    
    async def show_main_menu(self, message: types.Message):
        """Показать главное меню"""
        if not self.check_owner(message.from_user.id):
            return
            
        await message.answer(
            "🏠 Главное меню управления Discord ботом:",
            reply_markup=self.get_main_menu_keyboard()
        )
    
    async def main_menu_callback(self, callback: types.CallbackQuery):
        """Callback для возврата в главное меню"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await callback.message.edit_text(
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
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔄 Переключить", callback_data="toggle_auto_mark"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu"))
        
        await callback.message.edit_text(
            f"🔔 **Ежедневная автоотметка**\n\n"
            f"Автоматическая отправка сообщений в рабочие дни (пн-пт) с 10:30 до 12:00 МСК\n\n"
            f"Текущий статус: {status}",
            reply_markup=builder.as_markup(),
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
        builder.row(InlineKeyboardButton(text="🔄 Переключить еще раз", callback_data="toggle_auto_mark"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="auto_mark_menu"))
        
        await callback.message.edit_text(
            f"🔔 **Автоотметка {action}!**\n\n"
            f"Текущий статус: {status}",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer(f"Автоотметка {action}!")
        _log.info(f"Автоотметка {action} пользователем {callback.from_user.id}")
    
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
        
        await callback.message.edit_text(
            f"💬 **Текст ежедневной автоотметки**\n\n"
            f"Сообщение, которое автоматически отправляется в рабочие дни\n\n"
            f"Текущий текст: `{current_message}`",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def set_message_text_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало изменения текста сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await callback.message.edit_text(
            "✏️ Введите новый текст для автоотправки:",
            reply_markup=self.get_back_keyboard("message_settings_menu")
        )
        await state.set_state(BotStates.waiting_message_text)
        await callback.answer()
    
    async def process_message_text(self, message: types.Message, state: FSMContext):
        """Обработка нового текста сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        new_text = message.text.strip()
        self.discord_bot.set_chat_message = new_text
        
        await state.clear()
        await message.answer(
            f"✅ **Текст сообщения обновлен!**\n\n"
            f"Новый текст: `{new_text}`",
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
        
        await callback.message.edit_text(
            f"📅 **Отложить автоотметку до дня**\n\n"
            f"Приостановить ежедневную автоотметку до указанного числа месяца\n\n"
            f"Текущий день ожидания: {day_text}",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
    async def set_wait_day_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало установки дня ожидания"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        await callback.message.edit_text(
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
            day = int(message.text.strip())
            if not 1 <= day <= 31:
                await message.answer(
                    "❌ Номер дня должен быть от 1 до 31. Попробуйте еще раз:"
                )
                return
            
            self.discord_bot.set_day = day
            await state.clear()
            
            await message.answer(
                f"✅ **Автоотметка отложена!**\n\n"
                f"Ежедневная автоотметка приостановлена до {day} числа.",
                reply_markup=self.get_back_keyboard("wait_day_menu"),
                parse_mode="Markdown"
            )
            _log.info(f"День ожидания установлен на: {day}")
            
        except ValueError:
            await message.answer(
                "❌ Введите корректный номер дня (число от 1 до 31):"
            )
    
    async def clear_wait_day_callback(self, callback: types.CallbackQuery):
        """Очистка дня ожидания"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        self.discord_bot.set_day = None
        
        await callback.message.edit_text(
            f"✅ **Автоотметка возобновлена!**\n\n"
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
        
        await callback.message.edit_text(
            f"⏰ **Отложенные сообщения**\n\n"
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
            
        await callback.message.edit_text(
            "✏️ Введите текст отложенного сообщения:",
            reply_markup=self.get_back_keyboard("delayed_messages_menu")
        )
        await state.set_state(BotStates.waiting_delayed_message_text)
        await callback.answer()
    
    async def process_delayed_message_text(self, message: types.Message, state: FSMContext):
        """Обработка текста отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        text = message.text.strip()
        await state.update_data(delayed_message_text=text)
        
        await message.answer(
            f"📝 Текст сохранен: `{text}`\n\n"
            f"⏰ Теперь введите дату и время отправки.\n\n"
            f"**Форматы:**\n"
            f"• `ЧЧ:ММ` или `ЧЧ:ММ:СС` - только время (сегодня или завтра)\n"
            f"• `ДД.ММ ЧЧ:ММ` или `ДД.ММ ЧЧ:ММ:СС` - дата и время текущего года\n"
            f"• `ДД.ММ.ГГГГ ЧЧ:ММ` или `ДД.ММ.ГГГГ ЧЧ:ММ:СС` - полная дата и время\n\n"
            f"**Примеры:**\n"
            f"• `15:30` или `15:30:45`\n"
            f"• `25.12 18:00` или `25.12 18:00:30`\n"
            f"• `01.01.2025 00:00` или `01.01.2025 00:00:15`",
            reply_markup=self.get_back_keyboard("delayed_messages_menu"),
            parse_mode="Markdown"
        )
        await state.set_state(BotStates.waiting_delayed_message_datetime)
    
    async def process_delayed_message_datetime(self, message: types.Message, state: FSMContext):
        """Обработка даты и времени отложенного сообщения"""
        if not self.check_owner(message.from_user.id):
            return
            
        datetime_str = message.text.strip()
        
        try:
            target_datetime = self.parse_datetime_string(datetime_str)
            data = await state.get_data()
            text = data["delayed_message_text"]
            
            # Создаем отложенное сообщение
            message_id = self.next_message_id
            self.next_message_id += 1
            
            delayed_msg = DelayedMessage(
                id=message_id,
                text=text,
                date_time=target_datetime,
                created_at=datetime.now(self.moscow_tz)
            )
            
            self.delayed_messages[message_id] = delayed_msg
            
            # Запускаем задачу отправки
            task = asyncio.create_task(self.schedule_delayed_message(delayed_msg))
            self.delayed_tasks[message_id] = task
            
            await state.clear()
            
            await message.answer(
                f"✅ **Отложенное сообщение создано!**\n\n"
                f"📝 Текст: `{text}`\n"
                f"⏰ Время отправки: {target_datetime.strftime('%d.%m.%Y %H:%M:%S')} МСК",
                reply_markup=self.get_back_keyboard("delayed_messages_menu"),
                parse_mode="Markdown"
            )
            _log.info(f"Создано отложенное сообщение #{message_id} на {target_datetime}")
            
        except ValueError as e:
            await message.answer(f"❌ Ошибка в формате даты/времени: {e}\n\nПопробуйте еще раз:")
    
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
                    
                return moscow_now.replace(
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
            
            # Отправляем сообщение
            success = await self.discord_bot.send_message_to_channel(
                channel_id=self.discord_bot._private_channel_id,
                message_content=delayed_msg.text
            )
            
            if success:
                _log.info(f"Отложенное сообщение #{delayed_msg.id} успешно отправлено")
                # Уведомляем в телеграм
                try:
                    await self.bot.send_message(
                        self.owner_id,
                        f"✅ **Отложенное сообщение отправлено!**\n\n"
                        f"📝 Текст: `{delayed_msg.text}`\n"
                        f"⏰ Время: {delayed_msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    _log.error(f"Не удалось отправить уведомление о доставке: {e}")
            else:
                _log.error(f"Не удалось отправить отложенное сообщение #{delayed_msg.id}")
                # Уведомляем об ошибке
                try:
                    await self.bot.send_message(
                        self.owner_id,
                        f"❌ **Ошибка отправки отложенного сообщения!**\n\n"
                        f"📝 Текст: `{delayed_msg.text}`\n"
                        f"⏰ Время: {delayed_msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    _log.error(f"Не удалось отправить уведомление об ошибке: {e}")
            
            # Удаляем из хранилища
            if delayed_msg.id in self.delayed_messages:
                del self.delayed_messages[delayed_msg.id]
            if delayed_msg.id in self.delayed_tasks:
                del self.delayed_tasks[delayed_msg.id]
                
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
            await callback.message.edit_text(
                "📋 Отложенных сообщений нет.",
                reply_markup=self.get_back_keyboard("delayed_messages_menu")
            )
            await callback.answer()
            return
        
        # Сортируем по времени отправки
        sorted_messages = sorted(self.delayed_messages.values(), key=lambda x: x.date_time)
        
        text = "📋 **Отложенные сообщения:**\n\n"
        
        builder = InlineKeyboardBuilder()
        for msg in sorted_messages:
            # Ограничиваем длину текста для отображения
            preview_text = msg.text[:30] + "..." if len(msg.text) > 30 else msg.text
            text += f"**#{msg.id}** • {msg.date_time.strftime('%d.%m %H:%M')}\n`{preview_text}`\n\n"
            
            # Добавляем кнопки управления
            builder.row(
                InlineKeyboardButton(text=f"✏️ #{msg.id}", callback_data=f"edit_delayed_{msg.id}"),
                InlineKeyboardButton(text=f"🗑 #{msg.id}", callback_data=f"delete_delayed_{msg.id}")
            )
        
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="delayed_messages_menu"))
        
        await callback.message.edit_text(
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
        builder.row(InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit_text_{message_id}"))
        builder.row(InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_datetime_{message_id}"))
        builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_delayed_{message_id}"))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="view_delayed_messages"))
        
        await callback.message.edit_text(
            f"📝 **Редактирование сообщения #{message_id}**\n\n"
            f"**Текст:** `{msg.text}`\n"
            f"**Время отправки:** {msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК\n"
            f"**Создано:** {msg.created_at.strftime('%d.%m.%Y %H:%M:%S')} МСК",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        await callback.answer()
    
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
        
        await callback.message.edit_text(
            f"✏️ **Редактирование текста сообщения #{message_id}**\n\n"
            f"Текущий текст: `{msg.text}`\n\n"
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
        new_text = message.text.strip()
        
        if message_id not in self.delayed_messages:
            await message.answer("❌ Сообщение не найдено")
            await state.clear()
            return
        
        self.delayed_messages[message_id].text = new_text
        await state.clear()
        
        await message.answer(
            f"✅ **Текст сообщения #{message_id} обновлен!**\n\n"
            f"Новый текст: `{new_text}`",
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
        
        await callback.message.edit_text(
            f"⏰ **Редактирование времени сообщения #{message_id}**\n\n"
            f"Текущее время: {msg.date_time.strftime('%d.%m.%Y %H:%M:%S')} МСК\n\n"
            f"Введите новое время отправки:\n\n"
            f"**Форматы:**\n"
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
        datetime_str = message.text.strip()
        
        if message_id not in self.delayed_messages:
            await message.answer("❌ Сообщение не найдено")
            await state.clear()
            return
        
        try:
            new_datetime = self.parse_datetime_string(datetime_str)
            
            # Отменяем старую задачу
            if message_id in self.delayed_tasks:
                self.delayed_tasks[message_id].cancel()
                del self.delayed_tasks[message_id]
            
            # Обновляем время
            self.delayed_messages[message_id].date_time = new_datetime
            
            # Создаем новую задачу
            delayed_msg = self.delayed_messages[message_id]
            task = asyncio.create_task(self.schedule_delayed_message(delayed_msg))
            self.delayed_tasks[message_id] = task
            
            await state.clear()
            
            await message.answer(
                f"✅ **Время сообщения #{message_id} обновлено!**\n\n"
                f"Новое время: {new_datetime.strftime('%d.%m.%Y %H:%M:%S')} МСК",
                reply_markup=self.get_back_keyboard("view_delayed_messages"),
                parse_mode="Markdown"
            )
            _log.info(f"Время отложенного сообщения #{message_id} изменено на {new_datetime}")
            
        except ValueError as e:
            await message.answer(f"❌ Ошибка в формате даты/времени: {e}\n\nПопробуйте еще раз:")
    
    async def delete_delayed_message_callback(self, callback: types.CallbackQuery):
        """Удаление отложенного сообщения"""
        if not self.check_owner(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
            
        message_id = int(callback.data.split("_")[-1])
        
        if message_id not in self.delayed_messages:
            await callback.answer("❌ Сообщение не найдено")
            return
        
        # Отменяем задачу
        if message_id in self.delayed_tasks:
            self.delayed_tasks[message_id].cancel()
            del self.delayed_tasks[message_id]
        
        # Удаляем сообщение
        msg = self.delayed_messages[message_id]
        del self.delayed_messages[message_id]
        
        await callback.message.edit_text(
            f"✅ **Отложенное сообщение #{message_id} удалено!**\n\n"
            f"Удаленный текст: `{msg.text}`",
            reply_markup=self.get_back_keyboard("view_delayed_messages"),
            parse_mode="Markdown"
        )
        await callback.answer("Сообщение удалено!")
        _log.info(f"Отложенное сообщение #{message_id} удалено")
    
    async def start_polling(self):
        """Запуск бота"""
        _log.info("Запуск Telegram бота...")
        await self.dp.start_polling(self.bot)
    
    async def stop(self):
        """Остановка бота"""
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
    finally:
        # Корректно останавливаем бота
        if telegram_bot:
            try:
                await telegram_bot.stop()
            except Exception as e:
                _log.error(f"Ошибка при остановке Telegram бота: {e}")