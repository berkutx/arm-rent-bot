"""Run interactively once; credentials and login codes never enter command arguments."""
import asyncio,getpass,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from telethon import TelegramClient,errors
from source_sync import reader_lock

async def main():
    if not sys.stdin.isatty():raise SystemExit('Запустите скрипт в интерактивном терминале, без перенаправления ввода.')
    api_id=os.getenv('TELEGRAM_API_ID','');api_hash=os.getenv('TELEGRAM_API_HASH','')
    if not api_id.isdigit() or int(api_id)<=0 or not api_hash:
        raise SystemExit('Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в локальном .env (my.telegram.org → API development tools).')
    server.DATA.mkdir(parents=True,exist_ok=True);os.chmod(server.DATA,0o700)
    with reader_lock(server.DATA/'telegram-reader.lock'):
        path=server.DATA/'telegram-reader.session'
        client=TelegramClient(str(path),int(api_id),api_hash,receive_updates=False,entity_cache_limit=128,device_model='Armenia Rent Reader',app_version='0.5.0')
        client.session.save_entities=False
        try:
            await client.connect();os.chmod(path,0o600)
            if not await client.is_user_authorized():
                print('Используйте отдельный аккаунт: сохранённая сессия даёт доступ к его облачным чатам и отправке сообщений.')
                phone=getpass.getpass('Телефон аккаунта Telegram (+код страны): ')
                sent=await client.send_code_request(phone)
                try:
                    await client.sign_in(phone,code=getpass.getpass('Код из Telegram: '),phone_code_hash=sent.phone_code_hash)
                except errors.SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass('Пароль двухэтапной проверки: '))
            user=await client.get_me()
            if user.bot:raise SystemExit('Для чтения истории нужен пользовательский аккаунт.')
            print('Вход выполнен. Сессия сохранена в data/telegram-reader.session. Закройте этот процесс перед запуском синхронизации.')
        except errors.RPCError as exc:
            raise SystemExit('Telegram отклонил вход: '+type(exc).__name__) from None
        finally:await client.disconnect()

if __name__=='__main__':asyncio.run(main())
