import asyncio
import discord

from utils import load_env_config
from discord_bot import DiscordBot
from telegram_bot import run_telegram_bot
from custom_logger import setup_logging


_log = setup_logging()


async def main():
    _log.info("Загрузка конфигурации...")
    config = load_env_config()
    
    # Создаем Discord бота
    _log.info("Создание Discord бота...")
    
    discord_bot = DiscordBot(
        chat_channel_id=int(config['DISCORD_CHAT_CHANNEL_ID']),
        private_channel_id=int(config['DISCORD_PRIVATE_CHANNEL_ID'])
    )
    
    _log.info("Discord бот создан")
        
    # Создаем задачи для обоих ботов
    _log.info("Запуск ботов...")
    discord_task = asyncio.create_task(
        discord_bot.start(config['DISCORD_TOKEN']),
        name="discord_bot"
    )
    telegram_task = asyncio.create_task(
        run_telegram_bot(
            bot_token=config["TELEGRAM_TOKEN"],
            owner_id=config["YOUR_TELEGRAM_ID"],
            discord_bot=discord_bot
        ),
        name="telegram_bot"
    )
    
    # Запускаем оба бота одновременно
    try:
        _log.info("Оба бота запущены. Для остановки нажмите Ctrl+C")
        results = await asyncio.gather(
            discord_task, telegram_task, return_exceptions=True
        )
        
        # Проверяем результаты выполнения задач
        for i, result in enumerate(results):
            task_name = ["discord_bot", "telegram_bot"][i]
            if isinstance(result, Exception):
                _log.error(f"Ошибка в задаче {task_name}: {result}")
            else:
                _log.info(f"Задача {task_name} завершена нормально")
                
    except KeyboardInterrupt:
        _log.info("Получен сигнал остановки (Ctrl+C)")
        
        # Отменяем конкретные задачи ботов
        _log.info("Отмена задач ботов...")
        if not discord_task.done():
            discord_task.cancel()
        if not telegram_task.done():
            telegram_task.cancel()
            
        # Ждем завершения задач ботов
        try:
            await asyncio.gather(discord_task, telegram_task, return_exceptions=True)
        except Exception:
            pass  # Игнорируем ошибки отмены
            
    except discord.LoginFailure:
        _log.error("Ошибка авторизации Discord бота: проверьте правильность токена")
    except discord.HTTPException as e:
        _log.error(f"HTTP ошибка Discord: {e}")
    except Exception as e:
        _log.exception(f"Критическая ошибка при запуске ботов: {e}")
    finally:
        _log.info("Остановка ботов...")
        
        # Корректно останавливаем Discord бота
        try:
            if 'discord_bot' in locals() and not discord_bot.is_closed():
                await discord_bot.close()
                _log.info("Discord бот остановлен")
        except Exception as e:
            _log.error(f"Ошибка при остановке Discord бота: {e}")
        
        # Получаем текущую задачу (main)
        current_task = asyncio.current_task()
        
        # Отменяем все остальные активные задачи, кроме текущей
        tasks = [task for task in asyncio.all_tasks() 
                if not task.done() and task is not current_task]
        
        if tasks:
            _log.info(f"Отмена {len(tasks)} оставшихся задач...")
            for task in tasks:
                task.cancel()
            
            # Ждем завершения всех задач с таймаутом
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                _log.warning("Превышено время ожидания завершения задач")
            except Exception:
                pass  # Игнорируем ошибки при завершении
        
        _log.info("Все боты остановлены")


if __name__ == "__main__":
    asyncio.run(main())