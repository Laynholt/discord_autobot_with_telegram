import logging
from colorama import Fore, Style, init

# Инициализация colorama для Windows/Linux
init(autoreset=True)

# Словарь цветов по уровню
LOG_COLORS = {
    logging.DEBUG: Fore.GREEN,
    logging.INFO: Fore.BLUE,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
}

class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = LOG_COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{Style.RESET_ALL}"


def setup_logging() -> logging.Logger:
    """
    Настройка системы логирования для бота.
    Создает цветной форматированный вывод в консоль с временными метками.
    """
    # Форматтер с цветами
    formatter = ColorFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Консольный обработчик
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    
    # Настраиваем логгер для текущего модуля
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    
    # Настраиваем уровни для различных модулей
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)  # Убираем gateway спам
    logging.getLogger("discord.http").setLevel(logging.WARNING)     # Убираем HTTP спам
    logging.getLogger("discord.client").setLevel(logging.INFO)      # Оставляем важные сообщения клиента
    
    return logger
