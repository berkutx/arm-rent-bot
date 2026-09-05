"""Build or refresh only the demo database. Never seeds the live database or Telegram queue."""
import sys,json,hashlib
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import server

def seed():
 server.DATA=ROOT/'data';server.DB=server.DATA/'demo.sqlite3';server.setup()
 records=json.loads((ROOT/'seed.json').read_text(encoding='utf-8'))
 with server.db() as c:
  c.execute("DELETE FROM listings WHERE uid=0 AND json_extract(payload,'$.sample')=1")
  for l in records:
   created=datetime.fromisoformat(l['created_at']).timestamp()
   payload=server.dumps(l)
   c.execute('INSERT OR REPLACE INTO listings(id,uid,payload,status,created,confirmed,fingerprint,reason) VALUES(?,?,?,?,?,?,?,?)',
             (l['id'],0,payload,'active',created,None,hashlib.sha256(l['id'].encode()).hexdigest(),'Учебный пример. Не настоящее объявление.'))
  c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('demo_as_of','2026-09-05T11:00:00+04:00')")
  c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_release','0.5.0')")
  assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
 print(f'Seeded {len(records)} records → {server.DB}. Outgoing jobs: 0.')
if __name__=='__main__':seed()
