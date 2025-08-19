import sys
import pytz
import random
import logging
import asyncio
import discord
import calendar
from datetime import datetime, time, timedelta

from utils import load_env_config

_log = logging.getLogger(__name__)


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
        
        self._is_mark_enabled: bool = True
        self._wait_until_target_day: int | None = None
        
        self._chat_channel_message: str = "+"

    @property
    def wait_until_target_day(self) -> int | None:
        """Получить день, до которого бот будет ждать."""
        return self._wait_until_target_day
    
    @wait_until_target_day.setter
    def set_day(self, value: int | None) -> None:
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
    def set_chat_message(self, message: str) -> None:
        """Установить сообщение, которое отправляется при автоотправке."""
        self._chat_channel_message = message

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
            
        except discord.Forbidden:
            # Ошибка прав доступа - у бота нет разрешения на отправку сообщений
            _log.error("Отсутствуют права для отправки сообщений в канал %s", channel_id)
            return False
        except discord.HTTPException as e:
            # Ошибки API Discord (лимиты, проблемы с сетью и т.д.)
            _log.error("HTTP ошибка при отправке сообщения в канал %s: %s", channel_id, e)
            return False
        except Exception as e:
            # Любые другие непредвиденные ошибки
            _log.exception("Неожиданная ошибка при отправке сообщения в канал %s: %s", channel_id, e)
            return False

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
        start_seconds: int = (start_time.hour * 3600 + 
                             start_time.minute * 60 + 
                             start_time.second)
        end_seconds: int = (end_time.hour * 3600 + 
                           end_time.minute * 60 + 
                           end_time.second)
        
        # Генерируем случайное количество секунд в заданном диапазоне
        random_seconds: int = random.randint(start_seconds, end_seconds)
        
        # Конвертируем секунды обратно в часы, минуты и секунды
        hours: int = random_seconds // 3600
        minutes: int = (random_seconds % 3600) // 60
        seconds: int = random_seconds % 60
        
        generated_time = time(hours, minutes, seconds)
        _log.debug("Сгенерировано случайное время: %s", generated_time.strftime('%H:%M:%S'))
        return generated_time

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
        hours_to_wait = int(wait_seconds // 3600)
        minutes_to_wait = int((wait_seconds % 3600) // 60)
        seconds_to_wait = int(wait_seconds % 60)
        
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
        start_time: time = time(10, 30, 0)    # Начало временного окна (10:30:00)
        end_time: time = time(12, 0, 0)       # Конец временного окна (12:00:00)
        
        _log.info("Планировщик автоматических сообщений запущен")
        _log.info("Настройки: отправка в рабочие дни (пн-пт) с %s до %s МСК", 
                 start_time.strftime('%H:%M'), end_time.strftime('%H:%M'))
        
        # Основной цикл планировщика
        while True:
            try:
                # Получаем текущую дату и время в московском часовом поясе
                moscow_now: datetime = datetime.now(self.moscow_tz)
                _log.debug("Текущее время в Москве: %s", moscow_now.strftime('%Y-%m-%d %H:%M:%S'))
                
                start_datetime: datetime = moscow_now.replace(
                    hour=start_time.hour,
                    minute=start_time.minute,
                    second=start_time.second,
                    microsecond=0  # Обнуляем микросекунды для точности
                )
                end_datetime: datetime = moscow_now.replace(
                    hour=end_time.hour,
                    minute=end_time.minute,
                    second=end_time.second,
                    microsecond=0  # Обнуляем микросекунды для точности
                )
                
                if self._wait_until_target_day is not None:
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
                        target_datetime = datetime.combine(moscow_now.date(), start_time)
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
                    
                    target_date = datetime(target_year, target_month, self._wait_until_target_day, 
                                        start_time.hour, start_time.minute, start_time.second)
        
                    self._wait_until_target_day = None
                    await self.wait_until_next_date(target_date)
                    # Чтобы обновить все текущие даты
                    continue
                
                # Проверяем, является ли сегодня рабочим днем
                if self.is_weekday(moscow_now):
                    # Проверяем, что сейчас время в диапазоне
                    if start_datetime <= moscow_now <= end_datetime:
                        # Генерируем случайное время отправки в заданном диапазоне
                        target_time: time = self.get_random_time_in_range(
                            start_time=time(
                                moscow_now.hour,
                                moscow_now.minute,
                                moscow_now.second + 3
                            ),
                            end_time=end_time
                        )
                        
                        # Создаем полную дату и время для отправки сегодня
                        target_datetime: datetime = moscow_now.replace(
                            hour=target_time.hour,
                            minute=target_time.minute,
                            second=target_time.second,
                            microsecond=0  # Обнуляем микросекунды для точности
                        )
                        
                        # Проверяем, не прошло ли уже запланированное время сегодня
                        if target_datetime > moscow_now:
                            _log.info("Следующее сообщение запланировано на %s МСК", 
                                    target_datetime.strftime('%d.%m.%Y в %H:%M:%S'))
                            await self.wait_until_next_date(target_datetime)
                            
                            # Получаем точное время отправки для сообщения
                            current_moscow_time: datetime = datetime.now(self.moscow_tz)
                            
                            if self._is_mark_enabled:
                                _log.info("Отправка запланированного сообщения...")
                                
                                # Отправляем сообщение
                                success: bool = await self.send_message_to_channel(
                                    channel_id=self._chat_channel_id,
                                    message_content=self._chat_channel_message
                                )
                                
                                if success:
                                    _log.info("Автоматическое сообщение успешно отправлено в %s МСК", 
                                            current_moscow_time.strftime('%H:%M:%S'))
                                else:
                                    _log.error("Не удалось отправить запланированное сообщение")
                            else:
                                _log.info("Отправка отметок в чат отключена.")
                        
                        else:
                            # Если время уже прошло, выводим информацию
                            _log.info("Запланированное время (%s) уже прошло сегодня, ожидание следующего дня", 
                                    target_time.strftime('%H:%M:%S'))
                else:
                    # Сегодня выходной день
                    weekday_names: list[str] = [
                        'Понедельник', 'Вторник', 'Среда', 'Четверг', 
                        'Пятница', 'Суббота', 'Воскресенье'
                    ]
                    today_name: str = weekday_names[moscow_now.weekday()]
                    _log.info("Сегодня %s (выходной день), автоматические сообщения не отправляются", 
                             today_name)
                
                # Ждем до следующего диапазона
                await self.wait_until_next_date(start_datetime)
                
            except asyncio.CancelledError:
                # Обработка отмены задачи (например, при выключении бота)
                _log.info("Планировщик сообщений остановлен по запросу")
                break
                
            except Exception as e:
                # Обработка любых других ошибок в планировщике
                _log.exception("Критическая ошибка в планировщике сообщений: %s", e)
                _log.info("Попытка перезапуска планировщика через 5 минут...")
                await asyncio.sleep(300)  # 300 секунд = 5 минут


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