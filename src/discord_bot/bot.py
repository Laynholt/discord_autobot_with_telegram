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

# Настройки автоотправки
DEFAULT_CHAT_MESSAGE = "+"
WORK_DAY_START_HOUR = 10
WORK_DAY_START_MINUTE = 30
WORK_DAY_END_HOUR = 12
WORK_DAY_END_MINUTE = 0
WORK_DAY_END_SECOND_SHIFT = 3

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
        
        self._chat_channel_message: str = DEFAULT_CHAT_MESSAGE
        
        self._start_time: time = time(WORK_DAY_START_HOUR, WORK_DAY_START_MINUTE, 0)    # Начало временного окна
        self._end_time: time = time(WORK_DAY_END_HOUR, WORK_DAY_END_MINUTE, 0)       # Конец временного окна
        
        self._next_target_time: time | None = None
        self._next_target_time_locked: time | None = None
        self.regenerate_next_target_time()

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
            # Создаем naive datetime и затем локализуем его, чтобы избежать проблем с LMT/MSK
            target_datetime_naive = datetime.combine(target_date.date(), self._next_target_time)
            target_datetime = self.moscow_tz.localize(target_datetime_naive)
            return target_datetime.strftime('%H:%M:%S - %d.%m.%Y')
        
        # Если автоотправка отключена
        if not self._is_mark_enabled:
            return "Отключено"
        
        # Ищем следующий рабочий день
        current_date = moscow_now.date()
        for days_ahead in range(8):  # Максимум неделя вперед
            check_date = current_date + timedelta(days=days_ahead)
            # Создаем naive datetime и затем локализуем его, чтобы избежать проблем с LMT/MSK
            check_datetime_naive = datetime.combine(check_date, moscow_now.time())
            check_datetime = self.moscow_tz.localize(check_datetime_naive)
            
            if self.is_weekday(check_datetime):
                # Если это сегодня
                if days_ahead == 0:
                    # Проверяем, не прошло ли уже время отправки
                    if moscow_now.time() > self._next_target_time:
                        continue  # Переходим к следующему дню
                    # Если уже отправлялось сегодня, переходим к следующему дню
                    if self._was_sent_today and moscow_now.time() >= self._start_time:
                        continue
                # Создаем naive datetime для корректного форматирования
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
                start_datetime.second
            ),
            end_time=self._end_time
        )
        
    def regenerate_next_target_time(self) -> None:
        """
        Перегенерирует случайное время для следующей отправки сообщения.
        Используется для повторной генерации времени по требованию пользователя.
        """
        current_datetime: datetime = datetime.now(self.moscow_tz)
        start_datetime, end_datetime = self._create_time_range_for_date(
            current_datetime
        )
        
        # Если время еще не установлено (первоначальная генерация)
        if self._next_target_time is None:
            # Если мы в рабочем диапазоне, начинаем с текущего времени, иначе с начала диапазона
            _start_datetime = (
                current_datetime
                if start_datetime <= current_datetime <= end_datetime
                else start_datetime
            )
        else:
            # Если время уже установлено, генерируем новое время начиная с установленного момента
            # до конца рабочего диапазона
            locked_time = self._next_target_time_locked or self._next_target_time
            _start_datetime = current_datetime.replace(
                hour=locked_time.hour,
                minute=locked_time.minute,
                second=locked_time.second,
                microsecond=0,
            )
        
        # Генерируем новое случайное время
        self._next_target_time = self._initialize_next_target_time(_start_datetime)

    def set_next_target_time_once(self, next_time: time) -> None:
        """
        Устанавливает время следующей автоотправки вручную (одноразово).

        После отправки планировщик продолжит обычную случайную генерацию времени.
        """
        if not isinstance(next_time, time):
            raise TypeError("next_time должен быть datetime.time")

        if not (self._start_time <= next_time <= self._end_time):
            raise ValueError(
                "Время должно быть в диапазоне "
                f"{self._start_time.strftime('%H:%M:%S')} - {self._end_time.strftime('%H:%M:%S')} МСК"
            )

        self._next_target_time = next_time
        _log.info("Следующее время автоотправки вручную установлено на %s", next_time.strftime("%H:%M:%S"))

    def get_target_time_raw(self) -> time:
        """
        Возвращает текущее запланированное время отправки как объект time
        
        Returns:
            time: Время отправки
        """
        return self._next_target_time

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
    
    async def _reconnect_if_needed(self) -> None:
        """Переподключение к Discord при необходимости"""
        try:
            if self.is_closed():
                _log.info("Попытка переподключения к Discord...")
                await self.connect(reconnect=True)
        except Exception as e:
            _log.error("Ошибка при переподключении: %s", e)
    
    async def send_message_to_channel(self, channel_id: int, message_content: str) -> bool:
        """
        Отправляет сообщение в указанный канал по его ID
        
        Args:
            channel_id (int): Уникальный идентификатор канала Discord
            message_content (str): Текст сообщения для отправки
            
        Returns:
            bool: True если сообщение отправлено успешно, False в случае ошибки
        """
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                # Проверяем состояние подключения
                if self.is_closed():
                    _log.warning("Соединение закрыто, пытаемся переподключиться (попытка %d/%d)", attempt + 1, max_retries)
                    await self._reconnect_if_needed()
                    await asyncio.sleep(retry_delay)
                    continue
                
                # Получаем объект канала по его ID
                channel: discord.abc.Messageable | None = self.get_channel(channel_id) # type: ignore
                
                # Проверяем, что канал найден
                if channel is None:
                    _log.error("Канал с ID %s не найден", channel_id)
                    return False
                
                # Разбиваем текст на части если он слишком длинный
                text_parts = self._split_long_text(message_content)

                # Отправляем сообщение(я) в канал
                for index, text_part in enumerate(text_parts):
                    if index > 0:
                        await asyncio.sleep(SLEEP_DELAY_BETWEEN_MESSAGES)
                    await channel.send(text_part)

                channel_name = getattr(channel, 'name', f'ID:{channel_id}')
                _log.info("Сообщение успешно отправлено в канал '%s'", channel_name)
                return True
                
            except (discord.ConnectionClosed, ConnectionResetError, OSError) as e:
                _log.warning("Ошибка соединения (попытка %d/%d): %s", attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    return self._handle_message_send_error(e, channel_id, "")
                    
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
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                # Проверяем состояние подключения
                if self.is_closed():
                    _log.warning("Соединение закрыто, пытаемся переподключиться (попытка %d/%d)", attempt + 1, max_retries)
                    await self._reconnect_if_needed()
                    await asyncio.sleep(retry_delay)
                    continue
                
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
                
            except (discord.ConnectionClosed, ConnectionResetError, OSError) as e:
                _log.warning("Ошибка соединения при отправке с файлами (попытка %d/%d): %s", attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    return self._handle_message_send_error(e, channel_id, "с файлами")
                    
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
        elif isinstance(error, (discord.ConnectionClosed, ConnectionResetError, OSError)):
            _log.error("Ошибка подключения при отправке сообщения %s в канал %s: %s", error_type, channel_id, error)
        else:
            _log.exception("Неожиданная ошибка при отправке сообщения %s в канал %s: %s", error_type, channel_id, error)
        return False
    
    def _split_long_text(self, text: str, max_length: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
        """Разбивка длинного текста на части для Discord"""
        if len(text) <= max_length:
            return [text]
        
        parts: list[str] = []
        current_part = ""
        
        def append_part(part: str) -> None:
            cleaned = part.rstrip()
            # Discord не принимает пустые сообщения; пустые куски "теряют" первую часть
            # при отправке, если появились из-за переносов/пробелов.
            if cleaned:
                parts.append(cleaned)
        
        lines = text.split('\n')
        
        for line in lines:
            if len(line) > max_length:
                if current_part:
                    append_part(current_part)
                    current_part = ""
                
                words = line.split(' ')
                temp_line = ""
                
                for word in words:
                    candidate = word + " "

                    # Если слово не помещается в текущую часть, переносим его в следующую.
                    if len(temp_line + candidate) > max_length and temp_line:
                        append_part(temp_line)
                        temp_line = ""

                    # После переноса слово может поместиться целиком.
                    if len(candidate) <= max_length:
                        temp_line += candidate
                        continue

                    # Крайний случай: один токен длиннее лимита Discord (например, очень длинный URL/токен).
                    remaining_word = word
                    while len(remaining_word) > max_length:
                        parts.append(remaining_word[:max_length])
                        remaining_word = remaining_word[max_length:]
                    
                    if remaining_word:
                        temp_line = remaining_word + " "
                
                if temp_line:
                    current_part = temp_line
            else:
                if len(current_part + line + "\n") <= max_length:
                    current_part += line + "\n"
                else:
                    if current_part:
                        append_part(current_part)
                    current_part = line + "\n"
        
        if current_part:
            append_part(current_part)
        
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
        
        # Если начально время совпадает с конечным или больше его, то возвращаем конечное
        if start_seconds >= end_seconds:
            random_seconds = end_seconds        
        else:
            # Генерируем случайное количество секунд в заданном диапазоне
            # Сдвигаем на 3 секунды для надежности
            random_seconds: int = random.randint(start_seconds + WORK_DAY_END_SECOND_SHIFT, end_seconds)
        
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
            # Создаем naive datetime для сравнения времен
            target_datetime_naive = datetime.combine(moscow_now.date(), self._start_time)
            target_datetime = self.moscow_tz.localize(target_datetime_naive)
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

        # Убеждаемся, что оба datetime имеют одинаковую timezone информацию
        if next_datetime.tzinfo is None:
            next_datetime = self.moscow_tz.localize(next_datetime)
        elif next_datetime.tzinfo != self.moscow_tz:
            next_datetime = next_datetime.astimezone(self.moscow_tz)

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
        self._next_target_time_locked = self._start_time
        
        if self.is_weekday(moscow_now):
            if start_datetime <= moscow_now <= end_datetime: 
                await self._handle_workday_message_sending(start_datetime)
        else:
            self._log_weekend_message(moscow_now)
        
        # Ждем до начала следующего рабочего дня
        await self._wait_until_next_working_day(moscow_now)

    async def _handle_workday_message_sending(self, start_datetime: datetime) -> None:
        """
        Обрабатывает отправку сообщений в рабочие дни с возможностью повторной генерации времени
        
        Args:
            start_datetime: Время начала диапазона отправки
        """
        while True:
            current_moscow_time = datetime.now(self.moscow_tz)
            
            # Создаем целевое время для отправки
            target_datetime = current_moscow_time.replace(
                hour=self._next_target_time.hour,
                minute=self._next_target_time.minute,
                second=self._next_target_time.second,
                microsecond=0
            )
            
            # Проверяем, дождались ли мы нашего времени
            if target_datetime <= current_moscow_time:
                break
            
            self._next_target_time_locked = self._next_target_time
            # Время корректное, ждем и отправляем
            _log.info("Следующее сообщение запланировано на %s МСК", 
                     target_datetime.strftime('%d.%m.%Y в %H:%M:%S'))
            await self.wait_until_next_date(target_datetime)
        
        # Отправляем сообщение
        await self._send_scheduled_message()
        
        # Генерируем время для следующего дня и выходим
        self._next_target_time = self._initialize_next_target_time(start_datetime)

    async def _send_scheduled_message(self) -> None:
        """
        Отправляет запланированное сообщение
        """
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

    async def _wait_until_next_working_day(self, moscow_now: datetime) -> None:
        """
        Ожидает до следующего рабочего дня
        
        Args:
            moscow_now: Текущее время в московском часовом поясе
        """
        current_date = moscow_now.date()
        
        # Ищем следующий рабочий день
        for days_ahead in range(8):  # Максимум неделя вперед
            check_date = current_date + timedelta(days=days_ahead)
            # Создаем naive datetime и затем локализуем его, чтобы избежать проблем с LMT/MSK
            check_datetime_naive = datetime.combine(check_date, self._start_time)
            check_datetime = self.moscow_tz.localize(check_datetime_naive)
            
            if self.is_weekday(check_datetime):
                # Если это сегодня
                if days_ahead == 0:
                    # Проверяем, не прошло ли уже время отправки
                    if moscow_now.time() > self._start_time:
                        continue  # Переходим к следующему дню
                
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
