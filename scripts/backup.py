"""Consistent SQLite backup with private credentials removed by default.
The output contains public contacts and must still be protected.
Never takes the encryption key or copies WAL files blindly.
"""
import argparse,sqlite3,json,os
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 src=Path(a.database).resolve();dst=Path(a.output).resolve()
 if not src.is_file():raise SystemExit('Database not found')
 if src==dst or dst.exists():raise SystemExit('Refusing to overwrite a file or source database')
 dst.parent.mkdir(exist_ok=True,parents=True)
 with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(dst) as target:
  source.backup(target);target.execute('PRAGMA secure_delete=ON')
  target.execute('UPDATE listings SET private=NULL,private_expires=NULL')
  for lid,payload in target.execute('SELECT id,payload FROM listings').fetchall():
   d=json.loads(payload)
   if d.get('document_status')=='pending':
    d['document_status']='none';target.execute('UPDATE listings SET payload=? WHERE id=?',(json.dumps(d,ensure_ascii=False),lid))
  target.commit();target.execute('VACUUM')
 os.chmod(dst,0o600)
 print('Backup created without private credentials:',dst)
if __name__=='__main__':main()
