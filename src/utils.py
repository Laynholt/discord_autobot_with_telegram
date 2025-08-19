
import os
import logging

__all__ = ["load_env_config"]


_log = logging.getLogger(__name__)


def load_env_config() -> dict[str, str]:
    """
    Загружает конфигурацию из файла .env
    
    Returns:
        dict[str, str]: Словарь с конфигурационными данными
        
    Raises:
        FileNotFoundError: Если файл .env не найден
        ValueError: Если отсутствуют обязательные параметры
    """
    env_file = '.env'
    
    if not os.path.exists(env_file):
        _log.error('Файл .env не найден. Создайте файл .env со следующим содержимым:')
        _log.error('DISCORD_TOKEN=ваш_discord_токен (Получите по гайду [https://www.youtube.com/watch?v=ECHX8iZeC6o])')
        _log.error('DISCORD_CHAT_CHANNEL_ID=id_общего_канала')
        _log.error('DISCORD_PRIVATE_CHANNEL_ID=id_приватного_канала')
        _log.error('TELEGRAM_TOKEN=телеграм_токен_вашего_бота (Получите у @BotFather)')
        _log.error('YOUR_TELEGRAM_ID=ваш_телеграм_id')
        raise FileNotFoundError(f'Файл {env_file} не найден')
    
    config: dict[str, str] = {}
    required_keys = [
        'DISCORD_TOKEN', 'DISCORD_CHAT_CHANNEL_ID', 'DISCORD_PRIVATE_CHANNEL_ID',
        'TELEGRAM_TOKEN', 'YOUR_TELEGRAM_ID',
    ]
    
    try:
        with open(env_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                
                # Парсим строку в формате KEY=VALUE
                if '=' not in line:
                    _log.warning('Строка %d в .env игнорирована (неверный формат): %s', line_num, line)
                    continue
                
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"\'')  # Убираем кавычки если есть
                
                if key and value:
                    config[key] = value
                else:
                    _log.warning('Строка %d в .env игнорирована (пустой ключ или значение): %s', line_num, line)
    
    except Exception as e:
        _log.error('Ошибка при чтении файла .env: %s', e)
        raise IOError(f'Не удалось прочитать файл {env_file}: {e}')
    
    # Проверяем наличие обязательных параметров
    missing_keys = [key for key in required_keys if key not in config or not config[key]]
    if missing_keys:
        _log.error('В файле .env отсутствуют обязательные параметры: %s', ', '.join(missing_keys))
        raise ValueError(f'Отсутствуют обязательные параметры в .env: {", ".join(missing_keys)}')
    
    # Валидируем ID (должны быть числами)
    for channel_key in ['DISCORD_CHAT_CHANNEL_ID', 'DISCORD_PRIVATE_CHANNEL_ID', 'YOUR_TELEGRAM_ID']:
        try:
            int(config[channel_key])
        except ValueError:
            raise ValueError(f'{channel_key} должен быть числом, получено: {config[channel_key]}')
    
    _log.info('Конфигурация успешно загружена из .env')
    return config