// Optional PostgreSQL-in-WASM smoke test; see deployment guide.
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
const packageRoot=process.env.PGLITE_PACKAGE_ROOT;
const {PGlite}=await import(packageRoot ? pathToFileURL(path.join(packageRoot,'dist/index.js')).href : '@electric-sql/pglite');
const {pgcrypto}=await import(packageRoot ? pathToFileURL(path.join(packageRoot,'dist/contrib/pgcrypto.js')).href : '@electric-sql/pglite/contrib/pgcrypto');
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../',import.meta.url));
const db=new PGlite({extensions:{pgcrypto}});
try {
await db.exec(fs.readFileSync(root+'tests/sql/supabase_stub.sql','utf8'));
const schema=fs.readFileSync(root+'supabase/schema.sql','utf8');
await db.exec(schema);await db.exec(schema);
await db.exec(`grant usage on schema public, auth to anon, authenticated;
insert into auth.users(id,email) values ('11111111-1111-1111-1111-111111111111','a@x'),('22222222-2222-2222-2222-222222222222','b@x'),('33333333-3333-3333-3333-333333333333','c@x');
insert into profiles(id,username) select id,email from auth.users;
insert into conversations(id,created_by) overriding system value values (1,'11111111-1111-1111-1111-111111111111');
insert into conversation_participants(conversation_id,user_id) values (1,'11111111-1111-1111-1111-111111111111'),(1,'22222222-2222-2222-2222-222222222222');
insert into calls(id,conversation_id,caller_id,callee_id,call_type) overriding system value values (1,1,'11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','voice');
select set_config('realtime.topic','call-1',false);`);
async function asUser(uid,allowed,role='authenticated'){
 await db.query("select set_config('request.jwt.claim.sub',$1,false)",[uid]);
 await db.exec('set role '+role);
 try {
  if(allowed){await db.exec("insert into realtime.messages values ('call-1','broadcast')");assert.ok((await db.query('select * from realtime.messages')).rows.length>0);}
  else {assert.equal((await db.query('select * from realtime.messages')).rows.length,0);await assert.rejects(db.exec("insert into realtime.messages values ('call-1','broadcast')"),e=>e.code==='42501');}
 }finally{await db.exec('reset role');}
}
await asUser('11111111-1111-1111-1111-111111111111',true);
await asUser('22222222-2222-2222-2222-222222222222',true);
await db.exec('create policy "old broad access" on realtime.messages for all to anon, authenticated using(true) with check(true)');
await asUser('33333333-3333-3333-3333-333333333333',false);
await asUser('',false,'anon');
await db.exec("update profiles set status='suspended' where id='11111111-1111-1111-1111-111111111111'");
await asUser('11111111-1111-1111-1111-111111111111',false);
await db.exec("update calls set status='ended'");
await asUser('22222222-2222-2222-2222-222222222222',false);
await db.exec("select * from add_block('first','11111111-1111-1111-1111-111111111111',1); select * from add_block('second','22222222-2222-2222-2222-222222222222',1)");
assert.equal((await db.query('select * from validate_chain()')).rows[0].is_valid,true);
await db.exec("update blocks set index=9 where index=1; update blocks set block_hash=compute_block_hash(index,created_at,message_hash,sender_id,conversation_id,previous_hash) where index=9");
assert.equal((await db.query('select * from validate_chain()')).rows[0].is_valid,false);
console.log('PASS: schema reapplied; both call peers allowed; outsider, anonymous, suspended and ended-call users denied despite broad policy; rehashed index gap rejected.');
} finally {await db.close();}
