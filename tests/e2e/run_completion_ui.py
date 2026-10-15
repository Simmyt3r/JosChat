"""Real browser encryption, rendering, group controls, profile and SW notifications.

Uses the local HTTP API fixture; SQL authorization is tested separately in CI.
Optional JOSCHAT_EVALUATION_OUTPUT records actual send-to-display timings for the
polling fallback (not a measurement of live Supabase Realtime).
"""
import base64
import json
import os
import statistics
import threading
import time
import uuid

from run_messaging_ui import app, BASE, USERS, CONVS, MESSAGES, MEDIA, register, unlock, send
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


def main():
    server=make_server("localhost",5056,app,threaded=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            a=browser.new_context(viewport={"width":390,"height":844},permissions=["notifications"])
            b=browser.new_context(permissions=["notifications"])
            alice=a.new_page();bob=b.new_page()
            for pg in (alice,bob):
                pg.on("pageerror",lambda e:errors.append(str(e)))
                pg.route("https://**",lambda r:r.abort())
                pg.route("https://res.cloudinary.com/demo/raw/upload/**",lambda r:r.fulfill(body=MEDIA[r.request.url.rsplit('/',1)[1]],headers={"Access-Control-Allow-Origin":"*"},content_type="application/octet-stream"))
            register(alice,"alice");register(bob,"bob")
            USERS['carol@example.com']={'id':str(uuid.uuid4()),'username':'carol','phone_number':'+2348000000001'}
            alice.click('#new-group-btn');alice.fill('#group-name','Friends');alice.fill('#group-usernames','bob carol');alice.click('#group-submit')
            alice.wait_for_selector('#unlock-panel:not([hidden])');unlock(alice)
            bob.evaluate('loadConversations()');bob.wait_for_selector('.conv');bob.click('.conv');bob.wait_for_selector('#unlock-panel:not([hidden])');unlock(bob)
            timings=[]
            for i in range(int(os.environ.get("JOSCHAT_EVALUATION_SAMPLES","5"))):
                text=f'delivery evaluation {i}';start=time.perf_counter();send(alice,text)
                bob.wait_for_function('(text)=>document.getElementById("messages").textContent.includes(text)',arg=text)
                timings.append(round((time.perf_counter()-start)*1000,3))
            if os.environ.get('JOSCHAT_EVALUATION_OUTPUT'):
                from pathlib import Path
                Path(os.environ['JOSCHAT_EVALUATION_OUTPUT']).write_text(json.dumps({'environment':'Local HTTP API fixture, real Chromium and AES-GCM; actual 4-second polling fallback; Supabase transport unavailable', 'samples_ms':timings,'mean_ms':round(statistics.mean(timings),3)},indent=2)+'\n')
            print('PASS measured send-to-recipient display through polling fallback',flush=True)
            image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6WQAAAAASUVORK5CYII=')
            alice.set_input_files('#media-file',{'name':'private.png','mimeType':'image/png','buffer':image});send(alice,'Encrypted picture')
            assert len(MEDIA)==1 and next(iter(MEDIA.values()))!=image
            bob.evaluate('refreshMessages()');bob.wait_for_selector('.media img')
            bob.wait_for_function('Array.from(document.querySelectorAll(".media img")).some(i=>i.complete&&i.naturalWidth===1)')
            assert bob.get_attribute('.media img','src').startswith('blob:')
            print('PASS encrypted binary upload, authenticated download and image rendering',flush=True)
            name=next(iter(MEDIA));changed=bytearray(MEDIA[name]);changed[-1]^=1;MEDIA[name]=bytes(changed)
            bob.evaluate('async()=>{mediaCache.clear();await renderAllMessages({forceStick:true});}')
            mid=MESSAGES[-1]['id'];bob.wait_for_selector(f'.seal[data-msg-id="{mid}"][data-state="tampered"]')
            bob.click('#verify-btn');bob.wait_for_function('!verifying')
            assert bob.get_attribute(f'.seal[data-msg-id="{mid}"]','data-state')=='tampered'
            print('PASS corrupted file cannot regain Verified badge through a ledger recheck',flush=True)
            alice.click('#manage-group-btn');alice.fill('#rename-group-name','Renamed friends');alice.click('#rename-group-form button')
            alice.wait_for_function('document.getElementById("conv-title").textContent==="Renamed friends"')
            alice.click('#manage-group-btn');alice.locator('#manage-member-list li',has_text='@carol').get_by_role('button',name='Remove').click()
            alice.wait_for_selector('#unlock-panel:not([hidden])');assert alice.is_hidden('#composer')
            assert len(CONVS[1]['members'])==2 and CONVS[1]['key_epoch']==2
            alice.fill('#conv-passphrase','a completely fresh group phrase');alice.click('#unlock-btn')
            alice.wait_for_function("!document.getElementById('unlock-btn').classList.contains('is-busy') && !document.getElementById('composer').hidden")
            assert 'delivery evaluation 0' in alice.inner_text('#messages')
            send(alice,'A message after membership changed')
            assert MESSAGES[-1]['encrypted_content'].startswith('g3.2.e2.')
            alice.click('#manage-group-btn');alice.fill('#add-member-name','carol');alice.click('#add-member-form button')
            alice.wait_for_function('currentGroupEpoch===3')
            alice.evaluate('showList()')
            alice.click('#profile-btn');alice.fill('#edit-username','alice_updated');alice.fill('#edit-phone','+2349039930006');alice.click('#edit-profile-form button')
            alice.wait_for_function('currentProfile.username==="alice_updated"')
            assert alice.inner_text('#whoami')=='@alice_updated'
            assert not alice.evaluate('document.documentElement.scrollWidth>innerWidth')
            print('PASS rename, remove/add with key rotation, profile edit and mobile layout',flush=True)
            owner=alice.evaluate('currentProfile.id')
            alice.evaluate('(owner)=>outboxRequest("NOTIFICATION_OWNER",{owner_id:owner})',owner)
            worker=a.service_workers[0]
            # Headless shell has no OS notification service; exercise the worker's
            # complete payload/ownership handling with its presentation boundary stubbed.
            worker.evaluate("""() => {
              self.__testNotices=[];
              self.registration.showNotification=async(title,options)=>{self.__testNotices.push({title,...options});};
              self.registration.getNotifications=async()=>self.__testNotices;
            }""")
            worker.evaluate('(owner)=>showPushNotification({owner_id:owner,kind:"message",conversation_id:1})',owner)
            notices=worker.evaluate('async()=> (await self.registration.getNotifications()).map(n=>({title:n.title,body:n.body,url:n.data.url}))')
            assert notices[0]['title']=='Joschat' and notices[0]['url']=='/app?conversation=1'
            assert notices[0]['body']=='You have a new encrypted message.'
            alice.evaluate('outboxRequest("NOTIFICATION_OWNER",{owner_id:null})')
            worker.evaluate('(owner)=>showPushNotification({owner_id:owner,kind:"call",conversation_id:1})',owner)
            assert worker.evaluate('async()=> (await self.registration.getNotifications()).length')==1
            print('PASS service-worker notification payload and signed-out owner filtering (OS presentation stubbed)',flush=True)
            alice.evaluate('showThread()')
            alice.click('#manage-group-btn');alice.click('#leave-group-btn');alice.wait_for_function('currentConversationId===null')
            assert CONVS[1]['created_by']==USERS['bob@example.com']['id']
            assert not errors,errors
            a.close();browser.close()
    finally:server.shutdown()


if __name__=='__main__':main()
