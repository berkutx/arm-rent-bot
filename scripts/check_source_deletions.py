"""Check deletions of the existing Telegram import without importing or messaging."""
import argparse,asyncio,json,sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='Сверить удаления существующего импорта Telegram.')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true',help='Применить подтверждённые удаления; по умолчанию только отчёт.')
    mode.add_argument('--dry-run',action='store_true',help='Показать отчёт без изменения каталога.')
    parser.add_argument('--expected-account-id',type=int,help='ID проверенного аккаунта; иначе TELEGRAM_READER_USER_ID.')
    args=parser.parse_args()
    import server as s
    from source_sync import check_source_deletions,deletion_error_details
    if not s.DB.is_file():parser.error('Existing application database is required')
    try:
        report=asyncio.run(check_source_deletions(s.SOURCE_CATALOG,s.DATA,apply=args.apply,expected_account_id=args.expected_account_id))
    except Exception as error:
        print(json.dumps({'ok':False,**deletion_error_details(error)}))
        raise SystemExit(1) from None
    print(json.dumps({'ok':True,**report},ensure_ascii=False))


if __name__=='__main__':main()
