import sys
import pytz
import random
import logging
import asyncio
import discord
import calendar
from pathlib import Path
from datetime import datetime, time, timedelta

from utils import load_env_config

_log = logging.getLogger(__name__)

# Константы
SECONDS_IN_HOUR = 3600
SECONDS_IN_MINUTE = 60
SCHEDULER_RESTART_DELAY_SECONDS = 300  # 5 минут
DISCORD_MESSAGE_MAX_LENGTH = 2000
MAX_FILES_PER_MESSAGE = 10
SLEEP_DELAY_BETWEEN_MESSAGES = 0.5


class DiscordBot(discord.Client):
    """
    Кастомный клиент Discord бота с функцией автоматической отправки сообщений
    в рабочие дни в случайное время
    """
    
    def __init__(self, chat_channel_id: int, private_channel_id: int, *args, **kwargs):
        """
        Инициализация клиента с дополнительными параметрами для планировщика
        
        Args:
            chat_channel_id: int, (int): ID канала для отправки сообщения
            private_channel_id: int, (int): ID личных сообщений для отправки отложенного сообщения
        """
        super().__init__(*args, **kwargs)
        # Флаг для предотвращения множественного запуска планировщика
        self.scheduler_running: bool = False
        # Часовой пояс Москвы для корректной работы с местным временем
        self.moscow_tz: pytz.BaseTzInfo = pytz.timezone('Europe/Moscow')
        
        self._chat_channel_id: int = chat_channel_id
        self._private_channel_id: int = private_channel_id
        
        self._was_sent_today = False
        self._is_mark_enabled: bool = True
        self._wait_until_target_day: int | None = None
        
        self._chat_channel_message: str = "+"
        
        self._start_time: time = time(10, 30, 0)    # Начало временного окна (10:30:00)
        self._end_time: time = time(12, 0, 0)       # Конец временного окна (12:00:00)
        
        current_datetime: datetime = datetime.now(self.moscow_tz)
        start_datetime, end_datetime = self._create_time_range_for_date(current_datetime)
        _start_datetime = current_datetime if start_datetime <= current_datetime <= end_datetime else start_datetime
        
        # Генерируем случайное время отправки в заданном диапазоне
        self._next_target_time: time = self._initialize_next_target_time(_start_datetime)

    @property
    def wait_until_target_day(self) -> int | None:
        """Получить день, до которого бот будет ждать."""
        return self._wait_until_target_day
    
    @wait_until_target_day.setter
    def wait_until_target_day(self, value: int | None) -> None:
        """Установить день, до которого бот будет ждать."""
        if not isinstance(value, int | None):
            raise TypeError("День должен быть целым числом или None!")
        self._wait_until_target_day = value

    @property
    def should_send_mark_message(self) -> bool:
        """Узнать, включена ли автоотправки отметок в чате."""
        return self._is_mark_enabled

    def enable_sending_in_chat(self) -> None:
        """Включить автоотправку отметок в чате."""
        self._is_mark_enabled = True

    def disable_sending_in_chat(self) -> None:
        """Выключить автоотправку отметок в чате."""
        self._is_mark_enabled = False

    @property
    def chat_channel_message(self) -> str:
        """Получить сообщение, которое отправляется при автоотправке."""
        return self._chat_channel_message
    
    @chat_channel_message.setter
    def chat_channel_message(self, message: str) -> None:
        """Установить сообщение, которое отправляется при автоотправке."""
        self._chat_channel_message = message

    @property
    def next_target_time(self) -> str:
        """Возвращает дату и время следующего автоотправления."""
        moscow_now = datetime.now(self.moscow_tz)
        
        # Если установлен wait_until_target_day, возвращаем эту дату + уже сгенерированное время
        if self._wait_until_target_day is not None:
            target_date = self._calculate_wait_until_target_date(moscow_now)
            target_datetime = datetime.combine(
                target_date.date(), 
                self._next_target_time,
                self.moscow_tz
            )
            return target_datetime.strftime('%H:%M:%S - %d.%m.%Y')
        
        # Если автоотправка отключена
        if not self._is_mark_enabled:
            return "Отключено"
        
        # Ищем следующий рабочий день
        current_date = moscow_now.date()
        for days_ahead in range(8):  # Максимум неделя вперед
            check_date = current_date + timedelta(days=days_ahead)
            check_datetime = datetime.combine(check_date, moscow_now.time(), self.moscow_tz)
            
            if self.is_weekday(check_datetime):
                # Если это сегодня
                if days_ahead == 0:
                    # Проверяем, не прошло ли уже время отправки
                    if moscow_now.time() > self._next_target_time:
                        continue  # Переходим к следующему дню
                    # Если уже отправлялось сегодня, переходим к следующему дню
                    if self._was_sent_today and moscow_now.time() >= self._start_time:
                        continue
                next_time = datetime.combine(check_date, self._next_target_time)
                return next_time.strftime('%H:%M:%S - %d.%m.%Y')
        
        return "Не определено"

    def _create_time_range_for_date(self, date_time: datetime) -> tuple[datetime, datetime]:
        """
        Создает диапазон времени для заданной даты на основе start_time и end_time
        
        Args:
            date_time: Дата и время для которых создается диапазон
            
        Returns:
            tuple: (start_datetime, end_datetime)
        """
        start_datetime = date_time.replace(
            hour=self._start_time.hour,
            minute=self._start_time.minute,
            second=self._start_time.second,
            microsecond=0
        )
        end_datetime = date_time.replace(
            hour=self._end_time.hour,
            minute=self._end_time.minute,
            second=self._end_time.second,
            microsecond=0
        )
        return start_datetime, end_datetime

    def _initialize_next_target_time(self, start_datetime: datetime) -> time:
        """
        Инициализирует следующее целевое время для отправки сообщений
        
        Args:
            start_datetime: Время начала диапазона
            
        Returns:
            time: Случайное время в заданном диапазоне
        """
        return self.get_random_time_in_range(
            start_time=time(
                start_datetime.hour,
                start_datetime.minute,
                start_datetime.second + 3
            ),
            end_time=self._end_time
        )

    async def on_ready(self) -> None:
        """
        Событие, которое срабатывает когда бот успешно подключается к Discord
        Запускает планировщик сообщений, если он еще не запущен
        """
        _log.info('Бот успешно подключен как %s', self.user)
        
        # Запускаем планировщик только один раз
        if not self.scheduler_running:
            # Создаем асинхронную задачу для планировщика сообщений
            self.loop.create_task(self.message_scheduler())
            self.scheduler_running = True
    
    async def on_error(self, event, *args, **kwargs):
        """Обработчик ошибок Discord бота"""
        _log.exception(f"Ошибка в событии {event}")
    
    async def on_disconnect(self):
        """Обработчик отключения от Discord"""
        _log.warning("Discord бот отключен от сервера")
    
    async def on_connect(self):
        """Обработчик подключения к Discord"""
        _log.info("Discord бот подключается к серверу...")
    
    async def send_message_to_channel(self, channel_id: int, message_content: str) -> bool:
        """
        Отправляет сообщение в указанный канал по его ID
        
        Args:
            channel_id (int): Уникальный идентификатор канала Discord
            message_content (str): Текст сообщения для отправки
            
        Returns:
            bool: True если сообщение отправлено успешно, False в случае ошибки
        """
        try:
            # Получаем объект канала по его ID
            channel: discord.abc.Messageable | None = self.get_channel(channel_id) # type: ignore
            
            # Проверяем, что канал найден
            if channel is None:
                _log.error("Канал с ID %s не найден", channel_id)
                return False
            
            # Отправляем сообщение в канал
            await channel.send(message_content)
            channel_name = getattr(channel, 'name', f'ID:{channel_id}')
            _log.info("Сообщение успешно отправлено в канал '%s'", channel_name)
            return True
            
        except (discord.Forbidden, discord.HTTPException, Exception) as e:
            return self._handle_message_send_error(e, channel_id, "")
    
    async def send_message_with_files_to_channel(
        self, 
        channel_id: int, 
        message_content: str, 
        file_paths: list[str]
    ) -> bool:
        """
        Отправляет сообщение с файлами в указанный канал
        
        Args:
            channel_id: ID канала Discord
            message_content: Текст сообщения
            file_paths: Список путей к файлам для отправки
            
        Returns:
            bool: True если сообщение отправлено успешно
        """
        try:
            channel: discord.abc.Messageable | None = self.get_channel(channel_id) # type: ignore
            
            if channel is None:
                _log.error("Канал с ID %s не найден", channel_id)
                return False
            
            # Разбиваем текст на части если он слишком длинный
            text_parts = self._split_long_text(message_content)
            
            # Разбиваем файлы на группы
            file_groups = self._split_files(file_paths, MAX_FILES_PER_MESSAGE)
            
            # Отправляем первую часть текста с первой группой файлов
            if file_groups:
                files = [discord.File(file_path) for file_path in file_groups[0] if Path(file_path).exists()]
                await channel.send(content=text_parts[0] if text_parts else "", files=files)
                
                # Отправляем остальные группы файлов
                for file_group in file_groups[1:]:
                    files = [discord.File(file_path) for file_path in file_group if Path(file_path).exists()]
                    await asyncio.sleep(SLEEP_DELAY_BETWEEN_MESSAGES)  # Задержка между сообщениями
                    await channel.send(files=files)
            else:
                # Если нет файлов, отправляем только текст
                await channel.send(content=text_parts[0] if text_parts else "")
            
            # Отправляем остальные части текста
            for text_part in text_parts[1:]:
                await asyncio.sleep(SLEEP_DELAY_BETWEEN_MESSAGES)  # Задержка между сообщениями
                await channel.send(content=text_part)
            
            return True
            
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, Exception) as e:
            return self._handle_message_send_error(e, channel_id, "с файлами")
    
    def _handle_message_send_error(self, error: Exception, channel_id: int, error_type: str) -> bool:
        """
        Централизованная обработка ошибок при отправке сообщений
        
        Args:
            error: Исключение которое произошло
            channel_id: ID канала в котором произошла ошибка
            error_type: Тип ошибки для логирования
            
        Returns:
            bool: Всегда False, так как произошла ошибка
        """
        if isinstance(error, discord.Forbidden):
            _log.error("Отсутствуют права для отправки сообщений %s в канал %s", error_type, channel_id)
        elif isinstance(error, discord.NotFound):
            _log.error("Канал %s не найден", channel_id)
        elif isinstance(error, discord.HTTPException):
            _log.error("HTTP ошибка при отправке сообщения %s в канал %s: %s", error_type, channel_id, error)
        else:
            _log.exception("Неожиданная ошибка при отправке сообщения %s в канал %s: %s", error_type, channel_id, error)
        return False
    
    def _split_long_text(self, text: str, max_length: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
        """Разбивка длинного текста на части для Discord"""
        if len(text) <= max_length:
            return [text]
        
        parts = []
        current_part = ""
        
        lines = text.split('\n')
        
        for line in lines:
            if len(line) > max_length:
                if current_part:
                    parts.append(current_part.rstrip())
                    current_part = ""
                
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
                if len(current_part + line + "\n") <= max_length:
                    current_part += line + "\n"
                else:
                    if current_part:
                        parts.append(current_part.rstrip())
                    current_part = line + "\n"
        
        if current_part:
            parts.append(current_part.rstrip())
        
        return parts if parts else [text[:max_length]]
    
    def _split_files(self, file_paths: list[str], max_per_message: int = MAX_FILES_PER_MESSAGE) -> list[list[str]]:
        """Разбивка файлов на группы для отправки несколькими сообщениями"""
        if len(file_paths) <= max_per_message:
            return [file_paths] if file_paths else []
        
        groups = []
        for i in range(0, len(file_paths), max_per_message):
            groups.append(file_paths[i:i + max_per_message])
        
        return groups

    def get_random_time_in_range(self, start_time: time, end_time: time) -> time:
        """
        Генерирует случайное время между заданными временными границами
        с точностью до секунды
        
        Args:
            start_time (datetime.time): Начальное время диапазона (например, 10:30:00)
            end_time (datetime.time): Конечное время диапазона (например, 12:00:00)
            
        Returns:
            datetime.time: Случайно сгенерированное время в заданном диапазоне
        """
        # Конвертируем время в количество секунд с начала дня
        # Это позволяет легко работать с диапазонами времени
        start_seconds: int = (start_time.hour * SECONDS_IN_HOUR + 
                             start_time.minute * SECONDS_IN_MINUTE + 
                             start_time.second)
        end_seconds: int = (end_time.hour * SECONDS_IN_HOUR + 
                           end_time.minute * SECONDS_IN_MINUTE + 
                           end_time.second)
        
        # Генерируем случайное количество секунд в заданном диапазоне
        random_seconds: int = random.randint(start_seconds, end_seconds)
        
        # Конвертируем секунды обратно в часы, минуты и секунды
        hours: int = random_seconds // SECONDS_IN_HOUR
        minutes: int = (random_seconds % SECONDS_IN_HOUR) // SECONDS_IN_MINUTE
        seconds: int = random_seconds % SECONDS_IN_MINUTE
        
        generated_time = time(hours, minutes, seconds)
        _log.debug("Сгенерировано случайное время: %s", generated_time.strftime('%H:%M:%S'))
        return generated_time

    def _calculate_wait_until_target_date(self, moscow_now: datetime) -> datetime:
        """
        Вычисляет целевую дату для ожидания до указанного дня месяца
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
            
        Returns:
            datetime: Целевая дата и время для ожидания
        """
        if self._wait_until_target_day is None:
            return datetime(moscow_now.year, moscow_now.month, moscow_now.day, 
                       moscow_now.hour, moscow_now.minute, moscow_now.second + 1)
        
        # Определяем целевой месяц и год
        if self._wait_until_target_day > moscow_now.day:
            # Проверяем, есть ли такой день в текущем месяце
            days_in_month = calendar.monthrange(moscow_now.year, moscow_now.month)[1]
            if self._wait_until_target_day <= days_in_month:
                target_month, target_year = moscow_now.month, moscow_now.year
            else:
                # Следующий месяц
                target_month = moscow_now.month + 1 if moscow_now.month < 12 else 1
                target_year = moscow_now.year if moscow_now.month < 12 else moscow_now.year + 1
        elif self._wait_until_target_day == moscow_now.day:
            # Если это сегодня, проверяем время
            target_datetime = datetime.combine(moscow_now.date(), self._start_time)
            if target_datetime > moscow_now:
                # Время еще не прошло сегодня
                target_month, target_year = moscow_now.month, moscow_now.year
            else:
                # Время уже прошло, берем следующий месяц
                target_month = moscow_now.month + 1 if moscow_now.month < 12 else 1
                target_year = moscow_now.year if moscow_now.month < 12 else moscow_now.year + 1
        else:
            # Следующий месяц
            target_month = moscow_now.month + 1 if moscow_now.month < 12 else 1
            target_year = moscow_now.year if moscow_now.month < 12 else moscow_now.year + 1
        
        return datetime(target_year, target_month, self._wait_until_target_day, 
                       self._start_time.hour, self._start_time.minute, self._start_time.second)

    def is_weekday(self, date: datetime) -> bool:
        """
        Проверяет, является ли указанная дата рабочим днем (понедельник-пятница)
        
        Args:
            date (datetime): Дата для проверки
            
        Returns:
            bool: True если это рабочий день (пн-пт), False если выходной (сб-вс)
        """
        # weekday() возвращает: 0=понедельник, 1=вторник, ..., 6=воскресенье
        # Рабочие дни: 0-4 (понедельник-пятница)
        is_working_day = date.weekday() < 5
        _log.debug("Проверка дня недели для %s: %s", 
                  date.strftime('%Y-%m-%d'), 
                  "рабочий день" if is_working_day else "выходной")
        return is_working_day

    async def wait_until_next_date(self, next_datetime: datetime) -> None:
        """
        Ожидает до переданной даты.
        Args:
            next_datetime (datetime): Дата и время, до которых ждать.
        """
        current_datetime: datetime = datetime.now(self.moscow_tz)
        
        # Если текущее время больше переданного, то ожидаем до следующего для по переданному времени
        if current_datetime > next_datetime:
            next_datetime += timedelta(days=1)
        
        time_difference = next_datetime - current_datetime
        wait_seconds: float = time_difference.total_seconds()
        
        # Форматируем время ожидания для удобного отображения
        hours_to_wait = int(wait_seconds // SECONDS_IN_HOUR)
        minutes_to_wait = int((wait_seconds % SECONDS_IN_HOUR) // SECONDS_IN_MINUTE)
        seconds_to_wait = int(wait_seconds % SECONDS_IN_MINUTE)
        
        _log.info("Время ожидания: %dч %dм %dс", 
                    hours_to_wait, minutes_to_wait, seconds_to_wait)
        
        # Ждем до запланированного времени
        await asyncio.sleep(wait_seconds)
        

    async def message_scheduler(self) -> None:
        """
        Основной планировщик сообщений
        Работает в бесконечном цикле и отправляет сообщения в случайное время
        в рабочие дни между 10:30 и 12:00 по московскому времени
        """    
        _log.info("Планировщик автоматических сообщений запущен")
        _log.info("Настройки: отправка в рабочие дни (пн-пт) с %s до %s МСК", 
                 self._start_time.strftime('%H:%M'), self._end_time.strftime('%H:%M'))
        
        # Основной цикл планировщика
        while True:
            try:
                self._was_sent_today = False
                # Получаем текущую дату и время в московском часовом поясе
                moscow_now: datetime = datetime.now(self.moscow_tz)
                _log.debug("Текущее время в Москве: %s", moscow_now.strftime('%Y-%m-%d %H:%M:%S'))
                
                start_datetime, end_datetime = self._create_time_range_for_date(moscow_now)
                
                if self._wait_until_target_day is not None:
                    await self._handle_wait_until_target_day(moscow_now)
                    continue
                
                await self._process_daily_schedule(moscow_now, start_datetime, end_datetime)
                
            except asyncio.CancelledError:
                # Обработка отмены задачи (например, при выключении бота)
                _log.info("Планировщик сообщений остановлен по запросу")
                break
                
            except Exception as e:
                # Обработка любых других ошибок в планировщике
                _log.exception("Критическая ошибка в планировщике сообщений: %s", e)
                _log.info("Попытка перезапуска планировщика через 5 минут...")
                await asyncio.sleep(SCHEDULER_RESTART_DELAY_SECONDS)

    async def _handle_wait_until_target_day(self, moscow_now: datetime) -> None:
        """
        Обрабатывает ожидание до целевого дня
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
        """
        target_date = self._calculate_wait_until_target_date(moscow_now)
        self._wait_until_target_day = None
        await self.wait_until_next_date(target_date)

    async def _process_daily_schedule(self, moscow_now: datetime, start_datetime: datetime, end_datetime: datetime) -> None:
        """
        Обрабатывает ежедневное планирование сообщений
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
            start_datetime: Время начала диапазона отправки
            end_datetime: Время окончания диапазона отправки
        """
        if self.is_weekday(moscow_now):
            if start_datetime <= moscow_now <= end_datetime: 
                await self._handle_workday_message_sending(moscow_now, start_datetime)
        else:
            self._log_weekend_message(moscow_now)
        
        # Ждем до начала следующего рабочего дня
        await self._wait_until_next_working_day(start_datetime)

    async def _handle_workday_message_sending(self, moscow_now: datetime, start_datetime: datetime) -> None:
        """
        Обрабатывает отправку сообщений в рабочие дни
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
            start_datetime: Время начала диапазона отправки
        """
        # Создаем полную дату и время для отправки
        target_datetime = moscow_now.replace(
            hour=self._next_target_time.hour,
            minute=self._next_target_time.minute,
            second=self._next_target_time.second,
            microsecond=0
        )
        
        # Генерируем случайное время для следующей отправки
        self._next_target_time = self._initialize_next_target_time(start_datetime)
        
        # Проверяем, не прошло ли уже запланированное время
        if target_datetime > moscow_now:
            await self._wait_and_send_message(target_datetime)
        else:
            _log.info("Запланированное время (%s) уже прошло сегодня, ожидание следующего дня", 
                     self._next_target_time.strftime('%H:%M:%S'))

    async def _wait_and_send_message(self, target_datetime: datetime) -> None:
        """
        Ожидает до целевого времени и отправляет сообщение
        
        Args:
            target_datetime: Время когда нужно отправить сообщение
        """
        _log.info("Следующее сообщение запланировано на %s МСК", 
                 target_datetime.strftime('%d.%m.%Y в %H:%M:%S'))
        await self.wait_until_next_date(target_datetime)
        
        current_moscow_time = datetime.now(self.moscow_tz)
        
        if self._is_mark_enabled:
            _log.info("Отправка запланированного сообщения...")
            
            success = await self.send_message_to_channel(
                channel_id=self._chat_channel_id,
                message_content=self._chat_channel_message
            )
            self._was_sent_today = True
            
            if success:
                _log.info("Автоматическое сообщение успешно отправлено в %s МСК", 
                         current_moscow_time.strftime('%H:%M:%S'))
            else:
                _log.error("Не удалось отправить запланированное сообщение")
        else:
            _log.info("Отправка отметок в чат отключена.")

    def _log_weekend_message(self, moscow_now: datetime) -> None:
        """
        Логирует сообщение о выходном дне
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
        """
        weekday_names = [
            'Понедельник', 'Вторник', 'Среда', 'Четверг', 
            'Пятница', 'Суббота', 'Воскресенье'
        ]
        today_name = weekday_names[moscow_now.weekday()]
        _log.info("Сегодня %s (выходной день), автоматические сообщения не отправляются", 
                 today_name)

    async def _wait_until_next_working_day(self, start_datetime: datetime) -> None:
        """
        Ожидает до следующего рабочего дня
        
        Args:
            start_datetime: Начальное время в московском часовом поясе
        """
        current_date = start_datetime.date()
        
        # Ищем следующий рабочий день
        for days_ahead in range(1, 8):  # Максимум неделя вперед
            check_date = current_date + timedelta(days=days_ahead)
            check_datetime = datetime.combine(check_date, self._start_time, self.moscow_tz)
            
            if self.is_weekday(check_datetime):
                _log.debug("Следующий рабочий день: %s", check_datetime.strftime('%Y-%m-%d %H:%M:%S'))
                await self.wait_until_next_date(check_datetime)
                return


# Главная функция запуска бота
def main() -> None:
    """
    Главная функция запуска Discord бота
    Настраивает логирование и запускает клиент
    """    
    try:
        # Загружаем конфигурацию из .env
        try:
            config = load_env_config()
        except (FileNotFoundError, IOError, ValueError) as e:
            _log.error("Ошибка загрузки конфигурации: %s", e)
            sys.exit(1)
        
        # Определяем ID канала
        chat_channel_id = int(config['DISCORD_CHAT_CHANNEL_ID'])          
        private_channel_id = int(config['DISCORD_PRIVATE_CHANNEL_ID'])          
        _log.info("🚀 Запуск Discord бота...")
        
        # Создание экземпляра клиента бота с параметрами
        client: DiscordBot = DiscordBot(
            chat_channel_id=chat_channel_id,
            private_channel_id=private_channel_id
        )

        # Запускаем бота с токеном из конфигурации
        client.run(config['DISCORD_TOKEN'])
        
    except KeyboardInterrupt:
        _log.info("Бот остановлен пользователем (Ctrl+C)")
        
    except discord.LoginFailure:
        _log.error("Ошибка авторизации: проверьте правильность токена в файле .env")
        
    except Exception as e:
        _log.exception("Критическая ошибка при запуске бота: %s", e)


# Точка входа в программу
if __name__ == "__main__":
    main()