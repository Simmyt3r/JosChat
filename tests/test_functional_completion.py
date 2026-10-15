"""Real SQL + real Flask routes; only Auth and external push/storage are mocked.

The database fixture explicitly permits DBA tampering so verification can be
tested independently from the append-only guards. Runs in Postgres 16 in CI.
"""
import base64
import json
import os
import uuid

import pytest
import psycopg2
from test_schema_integration import db, _conversation, U1, U2
from config import Config
from utils.integrity import payload_hash, receipt_signature, verify_receipts
from utils.notifications import valid_subscription

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
U3 = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def api(db, monkeypatch, client):
    monkeypatch.setattr(Config, "POSTGRES_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr(Config, "POSTGRES_SSLMODE", "disable")
    monkeypatch.setattr(Config, "LEDGER_LEGACY_END_INDEX", 0)
    class Auth:
        def get_user(self, token): return {"id": token}
    monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: Auth())
    def invoke(method, path, data=None, user=U1):
        return getattr(client, method)(path, json=data, headers={"Authorization": f"Bearer {user}"})
    return invoke


def send(api, conv, media=None):
    r = api("post", "/api/messages/send", {"conversation_id": conv, "encrypted_content": "e2.ciphertext",
        "encrypted_media": media, "client_message_id": str(uuid.uuid4())})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["message"]


def test_payload_hash_matches_sql_including_unicode_and_nulls(db):
    for fields in (("héllo|", "", None, None), ("a", "b|1:c", "u", "p"), ("a", None, None, None)):
        db.execute("SELECT compute_message_hash(%s,%s,%s,%s)", fields)
        assert db.fetchone()[0] == payload_hash(*fields)
    assert payload_hash("a", None) != payload_hash("a", "")
    assert payload_hash("a|b", "c") != payload_hash("a", "b|c")


def test_real_message_send_receipt_and_replay(api, db):
    c = _conversation(db)
    m = send(api, c, "encrypted-file-reference")
    assert m["verified"] is True and m["integrity_version"] == 2
    body = {k:m[k] for k in ("conversation_id", "encrypted_content", "encrypted_media", "client_message_id")}
    r = api("post", "/api/messages/send", body)
    assert r.status_code == 200 and r.get_json()["duplicate"]
    db.execute("SELECT count(*) FROM blocks")
    assert db.fetchone()[0] == 1
    history = api("get", f"/api/messages/{c}").get_json()["messages"]
    assert history[0]["verified"] is True


@pytest.mark.parametrize("field", ["encrypted_content", "encrypted_media", "block_hash"])
def test_payload_tampering_fails_message_and_full_audit(api, db, field):
    c = _conversation(db); m = send(api, c, "ref")
    db.execute(f"UPDATE messages SET {field} = 'changed' WHERE id = %s", (m["id"],))
    assert api("get", f"/api/messages/{c}").get_json()["messages"][0]["verified"] is False
    db.execute("SELECT is_valid FROM validate_chain()")
    assert db.fetchone()[0] is False


def test_deleted_message_is_reported_in_full_and_scoped_audits(api, db):
    c = _conversation(db); m = send(api,c)
    db.execute("DELETE FROM messages WHERE id = %s", (m["id"],))
    r = api("get", f"/api/messages/verify/{c}").get_json()
    assert r["missing_messages"] == 1 and r["all_verified"] is False
    db.execute("SELECT is_valid FROM validate_chain()")
    assert db.fetchone()[0] is False


def test_coherent_database_rewrite_fails_external_key_receipt(api, db):
    c = _conversation(db); m = send(api,c)
    db.execute("UPDATE messages SET encrypted_content = 'rewritten' WHERE id = %s", (m["id"],))
    db.execute("UPDATE blocks SET message_hash = %s WHERE index = %s", (payload_hash("rewritten"),m["block_index"]))
    db.execute("UPDATE blocks SET block_hash = compute_block_hash(index,created_at,message_hash,sender_id,conversation_id,previous_hash)")
    db.execute("UPDATE messages m SET block_hash = b.block_hash FROM blocks b WHERE m.block_index = b.index")
    db.execute("SELECT is_valid FROM validate_chain()")
    assert db.fetchone()[0] is True   # Coherent hash chains alone cannot detect this.
    assert api("get", f"/api/messages/{c}").get_json()["messages"][0]["verified"] is False
    db.execute("UPDATE profiles SET role = 'admin' WHERE id = %s", (U1,))
    assert api("get", "/api/admin/blockchain/validate").get_json()["is_valid"] is False


def test_append_only_guards_prevent_accidental_deletion_or_rewrite(api, db):
    c = _conversation(db); send(api,c)
    db.execute("ALTER TABLE blocks ENABLE TRIGGER immutable_history; ALTER TABLE messages ENABLE TRIGGER immutable_history")
    for sql in ("DELETE FROM messages", "UPDATE messages SET encrypted_content='changed'", "DELETE FROM blocks", "UPDATE blocks SET message_hash='changed'", "DELETE FROM private.message_receipts"):
        with pytest.raises(psycopg2.Error) as exc: db.execute(sql)
        assert exc.value.pgcode == "55000"


def group(api, db):
    _conversation(db)
    db.execute("INSERT INTO auth.users(id,email) VALUES (%s,'c@x.com');",(U3,))
    db.execute("INSERT INTO profiles(id,username) VALUES (%s,'carol')",(U3,))
    r=api("post","/api/conversations",{"is_group":True,"title":"Friends","participant_usernames":["bob","carol"]})
    assert r.status_code==201
    return r.get_json()["conversation"]["id"]


def test_group_owner_changes_and_epoch_rejects_old_queued_message(api, db):
    c=group(api,db)
    assert api("patch",f"/api/conversations/{c}",{"title":"Renamed"},user=U2).status_code==403
    assert api("patch",f"/api/conversations/{c}",{"title":"Renamed"}).get_json()["conversation"]["title"]=="Renamed"
    r=api("patch",f"/api/conversations/{c}",{"action":"remove","user_id":U3})
    assert r.get_json()["conversation"]["key_epoch"]==2
    assert api("get",f"/api/conversations/{c}",user=U3).status_code==403
    r=api("post","/api/messages/send",{"conversation_id":c,"encrypted_content":"old","group_epoch":1})
    assert r.status_code==409
    r=api("patch",f"/api/conversations/{c}",{"action":"add","username":"carol"})
    assert r.get_json()["conversation"]["key_epoch"]==3
    r=api("patch",f"/api/conversations/{c}",{"action":"leave"})
    assert r.status_code==200 and r.get_json()["left"]
    assert r.get_json()["conversation"]["created_by"]==U2


def test_profile_update_cannot_escalate_role(api, db):
    _conversation(db)
    r=api("patch","/api/auth/profile",{"username":"newname","phone_number":"09039930006","role":"admin"})
    assert r.status_code==200 and r.get_json()["profile"]["role"]=="user"
    assert r.get_json()["profile"]["phone_number"]=="+2349039930006"


def test_accept_is_separate_from_connected_and_unauthorized_peer_denied(api, db):
    c=_conversation(db)
    row=api("post","/api/calls/start",{"conversation_id":c,"callee_id":U2}).get_json()["call"]
    cid=row["id"]
    assert api("post",f"/api/calls/{cid}/connected").status_code==409
    r=api("post",f"/api/calls/{cid}/accept",user=U2)
    assert r.get_json()["call"]["status"]=="accepted" and r.get_json()["call"]["connected_at"] is None
    assert api("post",f"/api/calls/{cid}/connected",user=U2).get_json()["call"]["connected_at"]
    assert api("post",f"/api/calls/{cid}/end",user=U2).status_code==200
    assert api("post",f"/api/calls/{cid}/connected").status_code==409


def test_call_channel_rls_blocks_unrelated_user_and_ended_call(api, db):
    c=_conversation(db)
    cid=api("post","/api/calls/start",{"conversation_id":c,"callee_id":U2}).get_json()["call"]["id"]
    db.execute("SET ROLE authenticated")
    db.execute("SELECT set_config('realtime.topic',%s,false),set_config('request.jwt.claim.sub',%s,false)",(f"call-{cid}",U1))
    db.execute("SELECT private.is_call_member(realtime.topic())")
    assert db.fetchone()[0] is True
    db.execute("INSERT INTO realtime.messages(extension) VALUES ('broadcast')")
    db.execute("SELECT set_config('request.jwt.claim.sub',%s,false)",(U3,))
    with pytest.raises(psycopg2.errors.InsufficientPrivilege): db.execute("INSERT INTO realtime.messages(extension) VALUES ('broadcast')")
    db.execute("RESET ROLE")
    api("post",f"/api/calls/{cid}/end")
    db.execute("SELECT set_config('request.jwt.claim.sub',%s,false)",(U1,))
    db.execute("SELECT private.is_call_member(%s)",(f"call-{cid}",))
    assert db.fetchone()[0] is False


def subscription():
    return {"endpoint":"https://fcm.googleapis.com/fcm/send/test", "keys":{
        "p256dh":base64.urlsafe_b64encode(b'\x04'+b'a'*64).decode(), "auth":base64.urlsafe_b64encode(b'b'*16).decode()}}


def test_private_notification_delivery_and_ownership(api, db, monkeypatch):
    c=_conversation(db); monkeypatch.setattr(Config,"VAPID_PRIVATE_KEY","test-key")
    pushes=[]
    monkeypatch.setattr("utils.notifications.webpush",lambda **kw: pushes.append(kw))
    sub=subscription()
    assert api("post","/api/notifications/subscriptions",sub,user=U2).status_code==201
    assert api("post","/api/notifications/subscriptions",sub).status_code==409
    send(api,c)
    assert len(pushes)==1
    data=json.loads(pushes[0]["data"])
    assert data=={"owner_id":U2,"kind":"message","conversation_id":c}
    assert "ciphertext" not in pushes[0]["data"]
    api("delete","/api/notifications/subscriptions",{"endpoint":sub["endpoint"]})
    db.execute("SELECT count(*) FROM private.push_subscriptions")
    assert db.fetchone()[0]==1


@pytest.mark.parametrize("url",["http://fcm.googleapis.com/x","https://127.0.0.1/x","https://fcm.googleapis.com.evil.test/x","https://evil.test/x","https://user@fcm.googleapis.com/x","https://fcm.googleapis.com:8443/x"])
def test_push_endpoint_validation_prevents_ssrf(url):
    data=subscription();data['endpoint']=url
    assert not valid_subscription(data)


def test_scheduled_audit_and_export_authorization(api, db, client):
    c=_conversation(db);send(api,c)
    assert client.get('/api/system/maintenance').status_code==401
    r=client.get('/api/system/maintenance',headers={"Authorization":f"Bearer {Config.CRON_SECRET}"})
    assert r.status_code==200 and r.get_json()['integrity']['is_valid']
    assert api('get','/api/admin/report').status_code==403
    db.execute("UPDATE profiles SET role='admin' WHERE id=%s",(U1,))
    r=api('get','/api/admin/report')
    assert r.status_code==200 and 'attachment;' in r.headers['Content-Disposition']
    assert api('get','/api/admin/audits').get_json()['audits'][0]['source']=='export'


def test_legacy_attachment_is_preserved_and_receipt_binds_its_reference(api, db, client, monkeypatch):
    import hashlib
    c=_conversation(db)
    db.execute("select idx,block_hash from add_block(%s,%s,%s)",(hashlib.sha256(b"old").hexdigest(),U1,c))
    index,block_hash=db.fetchone()
    db.execute("insert into messages(conversation_id,sender_id,encrypted_content,encrypted_media,block_index,block_hash) values (%s,%s,'old','original legacy reference',%s,%s)",(c,U1,index,block_hash))
    monkeypatch.setattr(Config,'LEDGER_LEGACY_END_INDEX',1)
    response=client.post('/api/system/seal-history',headers={'Authorization':f'Bearer {Config.CRON_SECRET}'})
    assert response.status_code==200 and response.get_json()['sealed']==1
    row=api('get',f'/api/messages/{c}').get_json()['messages'][0]
    assert row['verified'] is True and row['legacy_attachment'] is True
    db.execute("update messages set encrypted_media='changed reference'")
    assert api('get',f'/api/messages/{c}').get_json()['messages'][0]['verified'] is False


def test_group_replay_stays_idempotent_after_membership_rotation(api,db):
    c=group(api,db)
    body={'conversation_id':c,'encrypted_content':'g3.1.e2.ciphertext','group_epoch':1,'client_message_id':str(uuid.uuid4())}
    first=api('post','/api/messages/send',body)
    assert first.status_code==201
    api('patch',f'/api/conversations/{c}',{'action':'remove','user_id':U3})
    replay=api('post','/api/messages/send',body)
    assert replay.status_code==200 and replay.get_json()['message']['id']==first.get_json()['message']['id']


def test_push_retries_are_protected_and_failures_stay_durable(api,db,client,monkeypatch):
    from utils.notifications import WebPushException
    c=_conversation(db);monkeypatch.setattr(Config,'VAPID_PRIVATE_KEY','test-key')
    api('post','/api/notifications/subscriptions',subscription(),user=U2)
    def fail(**kwargs): raise WebPushException('retry')
    monkeypatch.setattr('utils.notifications.webpush',fail)
    send(api,c)
    db.execute('select attempts from private.push_deliveries')
    assert db.fetchone()[0]==1
    assert client.get('/api/system/notifications').status_code==401
    monkeypatch.setattr('utils.notifications.webpush',lambda **kwargs:None)
    response=client.get('/api/system/notifications',headers={'Authorization':f'Bearer {Config.CRON_SECRET}'})
    assert response.status_code==200 and response.get_json()['sent']==1
    db.execute('select count(*) from private.push_deliveries')
    assert db.fetchone()[0]==0


def test_signed_upload_is_scoped_and_does_not_expose_secret(api,db):
    import cloudinary.utils
    c=_conversation(db)
    result=api('post','/api/media/sign-upload',{'conversation_id':c})
    assert result.status_code==200
    payload=result.get_json();fields=dict(payload['fields']);signature=fields.pop('signature');fields.pop('api_key')
    assert payload['upload_url']=='https://api.cloudinary.com/v1_1/demo/raw/upload'
    assert fields['public_id'].startswith(f'joschat/{c}/') and fields['public_id'].endswith('.jcm')
    assert fields['overwrite']=='false'
    assert signature==cloudinary.utils.api_sign_request(fields,Config.CLOUDINARY_API_SECRET)
    assert Config.CLOUDINARY_API_SECRET not in json.dumps(payload)
    db.execute('delete from conversation_participants where user_id=%s',(U2,))
    assert api('post','/api/media/sign-upload',{'conversation_id':c},user=U2).status_code==403
