"""Reproduce dissertation §3.6 integrity metrics on a disposable Postgres.

TEST_DATABASE_URL must contain 'test': all application schemas are recreated.
Run: TEST_DATABASE_URL=... python tests/evaluate_integrity.py --output evaluations/integrity.json
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LEDGER_SIGNING_KEY", "evaluation-only-signing-key")
from config import Config
from utils.integrity import payload_hash, receipt_signature, run_audit
import psycopg2
from psycopg2.extras import RealDictCursor


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output", required=True); args=parser.parse_args()
    url=os.environ["TEST_DATABASE_URL"]
    assert 'test' in url.rsplit('/',1)[-1].split('?')[0].lower(), 'Disposable test database required'
    conn=psycopg2.connect(url,cursor_factory=RealDictCursor);conn.autocommit=True;cur=conn.cursor()
    cur.execute("drop schema public cascade; create schema public; drop schema if exists auth cascade; drop schema if exists private cascade; drop schema if exists realtime cascade")
    cur.execute((ROOT/'tests/sql/supabase_stub.sql').read_text());cur.execute((ROOT/'supabase/schema.sql').read_text())
    uid='11111111-1111-1111-1111-111111111111'
    cur.execute("insert into auth.users(id,email) values (%s,'evaluation@example.invalid')",(uid,))
    cur.execute("insert into profiles(id,username) values (%s,'evaluation')",(uid,))
    cur.execute("insert into conversations(created_by) values (%s) returning id",(uid,));cid=cur.fetchone()['id']
    cur.execute("alter table blocks disable trigger immutable_history; alter table messages disable trigger immutable_history")
    results=[];total=0
    for size in (10,100,1000):
        while total<size:
            content=f'encrypted-evaluation-{total}';media='encrypted-file-reference'
            cur.execute("select * from add_block(%s,%s,%s)",(payload_hash(content,media),uid,cid));b=cur.fetchone()
            cur.execute("insert into messages(conversation_id,sender_id,encrypted_content,encrypted_media,block_index,block_hash,integrity_version) values (%s,%s,%s,%s,%s,%s,2) returning id",(cid,uid,content,media,b['idx'],b['block_hash']))
            mid=cur.fetchone()['id']
            cur.execute("insert into private.message_receipts(block_index,message_id,signature,payload_hash) values (%s,%s,%s,%s)",(b['idx'],mid,receipt_signature(b['idx'],b['block_hash'],mid,payload_hash(content,media)),payload_hash(content,media)))
            total+=1
        durations=[]
        for _ in range(5):
            start=time.perf_counter();report=run_audit(cur,'evaluation');durations.append((time.perf_counter()-start)*1000)
            assert report['is_valid']
        results.append({'blocks':size,'repeats':5,'mean_ms':round(statistics.mean(durations),3),'min_ms':round(min(durations),3),'max_ms':round(max(durations),3)})
        print(f'Validated {size} blocks',flush=True)
    cur.execute("select count(*) as n, count(*) filter (where not validate_message(id)) as false_positives from messages")
    unchanged=cur.fetchone()
    cases={
        'text_ciphertext':"update messages set encrypted_content='altered' where block_index=0",
        'attachment_reference':"update messages set encrypted_media='altered' where block_index=0",
        'message_deleted':"delete from messages where block_index=0",
        'block_hash':"update blocks set block_hash='altered' where index=0",
        'tail_deleted':"delete from messages where block_index=999; delete from blocks where index=999",
        'index_gap':"delete from messages where block_index=500; delete from blocks where index=500",
        'coherent_database_rewrite':"""update messages set encrypted_content='altered' where block_index=999;
            update blocks b set message_hash=compute_message_hash(m.encrypted_content,m.encrypted_media,m.media_url,m.media_public_id)
              from messages m where m.block_index=b.index and b.index=999;
            update blocks set block_hash=compute_block_hash(index,created_at,message_hash,sender_id,conversation_id,previous_hash) where index=999;
            update messages m set block_hash=b.block_hash from blocks b where m.block_index=b.index and b.index=999;""",
    }
    checks=[]
    for name,sql in cases.items():
        cur.execute('begin')
        try:
            cur.execute(sql);report=run_audit(cur,'tamper-evaluation')
            checks.append({'case':name,'detected':not report['is_valid']})
        finally:cur.execute('rollback')
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'environment':'Disposable local SQL engine; not a Jos mobile-network pilot',
        'sql_version':None,'tamper_detection':{'detected':sum(c['detected'] for c in checks),'attempted':len(checks),'cases':checks},
        'false_positive_rate':{'flagged':unchanged['false_positives'],'untampered':unchanged['n']},'chain_validation':results}
    cur.execute('select version() as version');report['sql_version']=cur.fetchone()['version']
    Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(report,indent=2)+'\n');conn.close()
    assert all(c['detected'] for c in checks) and not unchanged['false_positives']


if __name__=='__main__': main()
