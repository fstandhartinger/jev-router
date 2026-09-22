import base64, hashlib, hmac, json, sqlite3, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import app

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"test.db"))
    with TestClient(app.app) as c: yield c

def make_user_key():
    raw="jvr_test_key"
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("test@example.com","Test","sub",app.now_iso()))
        uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO api_keys(user_id,prefix,key_hash,name,created_at) VALUES(?,?,?,?,?)",(uid,raw[:12],app.hash_key(raw),"Test",app.now_iso()))
    return uid,raw

def test_public_pages_and_model_neutrality(client):
    for path in ("/","/health","/models","/models-page","/docs","/status","/terms","/privacy","/refunds","/impressum"):
        assert client.get(path).status_code==200
    data=client.get("/models").json()
    assert "jev-class uses descending text JevBench" in data["routing_policy"]
    assert next(x for x in data["data"] if x["id"]=="jev-class")["routing_order"]==["classifier-fast"]
    assert not any(x["id"] in ("jev-typesafe","jev-vercel") for x in data["data"])
    text=client.get("/models-page").text
    assert "TypeSafe" not in text and "Vercel" not in text
    assert "Open decision models" in text

def test_home_leads_with_live_shared_api_when_hosting_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(app,"HOSTING_CONTROL_URL","")
    monkeypatch.setattr(app,"HOSTING_CONTROL_TOKEN","")
    response=client.get("/")
    assert response.status_code==200
    assert "Use the live API" in response.text
    assert "Dedicated hosting is currently unavailable" in response.text
    assert 'href="/login">Start a model' not in response.text

def test_models_page_marks_dedicated_hosting_unavailable(client, monkeypatch):
    monkeypatch.setattr(app,"HOSTING_CONTROL_URL","")
    monkeypatch.setattr(app,"HOSTING_CONTROL_TOKEN","")
    response=client.get("/models-page")
    assert response.status_code==200
    assert "Availability" in response.text
    assert response.text.count('<span class="badge offline">unavailable</span>')==len(app.ON_DEMAND_MODELS)
    assert "Shared routes below remain usable" in response.text

def test_mobile_navigation_wraps_instead_of_scrolling(client):
    response=client.get("/")
    assert "nav{gap:12px;flex-wrap:wrap;padding:16px}" in response.text
    assert "nav{gap:12px;overflow:auto}" not in response.text

def test_unconfigured_hosting_does_not_start_reaper(client):
    assert not app.HOSTING_CONTROL_URL
    assert not app.HOSTING_CONTROL_TOKEN
    assert app.HOSTING_TASK is None

def test_browser_favicon_request_does_not_error(client):
    assert client.get("/favicon.ico").status_code==204

def test_auth_required_and_explicit_model(client):
    assert client.post("/v1/systemone",json={}).status_code==401
    _,key=make_user_key()
    q={"q":{"type":"choice","instructions":"Choose","criteria":{"a":"A","b":"B"}}}
    r=client.post("/v1/systemone",headers={"Authorization":"Bearer "+key},json={"state":"x","questions":q})
    assert r.status_code==400 and r.json()["detail"]["error"]=="model_required"

def test_multimodal_validation(client):
    _,key=make_user_key(); headers={"Authorization":"Bearer "+key}
    image="data:image/png;base64,"+base64.b64encode(b"tiny").decode()
    payload={"model":"semif-qwen3.5-4b","state":"x","images":[image],"questions":{"q":{"type":"choice","instructions":"Choose","criteria":{"a":"A","b":"B"}}}}
    r=client.post("/v1/multimodal",headers=headers,json=payload)
    assert r.status_code==400 and r.json()["detail"]["error"]=="model_not_multimodal"
    payload["model"]="djev"; payload["images"]=["https://example.com/a.png"]
    assert client.post("/v1/multimodal",headers=headers,json=payload).status_code==400

def test_typed_contract_validation(client):
    _,key=make_user_key(); headers={"Authorization":"Bearer "+key}
    assert client.post("/v1/systemone",headers=headers,json={"model":"classifier-fast","questions":{"q":{"type":"choice","criteria":{"a":None,"b":None}}}}).status_code==400
    assert client.post("/v1/systemone",headers=headers,json={"model":"classifier-fast","state":"x","questions":{"q":{"type":"unknown"}}}).status_code==400
    assert client.post("/v1/systemone",headers=headers,json={"model":"classifier-fast","fallback":"djev","state":"x","questions":{"q":{"type":"noul"}}}).status_code==400
    assert client.post("/v1/systemone",headers=headers,json={"model":"classifier-fast","state":"x","questions":{"q":{"type":"choice","criteria":["a","b"]}}}).status_code==400
    assert client.post("/v1/systemone",headers=headers,json={"model":"classifier-fast","state":"x","questions":{"q":{"type":"score","criteria":["low","high"]}}}).status_code==400
    image="data:image/png;base64,"+base64.b64encode(b"tiny").decode()
    assert client.post("/v1/systemone",headers=headers,json={"model":"djev","state":"x","images":[image],"questions":{"q":{"type":"choice","criteria":{"a":None,"b":None}}}}).status_code==400

def test_key_revocation(client):
    uid,key=make_user_key()
    assert app.api_user("Bearer "+key)["id"]==uid
    with app.dbconn() as db: db.execute("UPDATE api_keys SET revoked_at=?",(app.now_iso(),))
    with pytest.raises(Exception): app.api_user("Bearer "+key)

def test_stripe_signature_and_idempotent_credit(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    event={"id":"evt_1","type":"checkout.session.completed","data":{"object":{"id":"cs_1","payment_intent":"pi_1","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}}
    raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
    headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"}
    assert client.post("/webhooks/stripe",content=raw,headers=headers).status_code==200
    assert client.post("/webhooks/stripe",content=raw,headers=headers).json()["duplicate"] is True
    assert app.balance(uid)==10_000_000

    refund={"id":"evt_2","type":"charge.refunded","data":{"object":{"payment_intent":"pi_1","amount_refunded":250}}}
    raw=json.dumps(refund,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
    assert client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"}).status_code==200
    assert app.balance(uid)==7_500_000

def test_out_of_order_stripe_refund_is_applied(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    assert send({"id":"evt_early","type":"charge.refunded","data":{"object":{"payment_intent":"pi_late","amount_refunded":250}}}).status_code==200
    assert app.balance(uid)==0
    assert send({"id":"evt_checkout","type":"checkout.session.completed","data":{"object":{"id":"cs_late","payment_intent":"pi_late","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}}).status_code==200
    assert app.balance(uid)==7_500_000

def test_won_dispute_restores_credit(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_top","type":"checkout.session.completed","data":{"object":{"id":"cs_top","payment_intent":"pi_d","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    send({"id":"evt_open","type":"charge.dispute.created","data":{"object":{"id":"dp_1","payment_intent":"pi_d","amount":400}}})
    assert app.balance(uid)==6_000_000
    send({"id":"evt_won","type":"charge.dispute.closed","data":{"object":{"id":"dp_1","payment_intent":"pi_d","amount":400,"status":"won"}}})
    assert app.balance(uid)==10_000_000

def test_full_refund_and_won_dispute_cannot_restore_refunded_credit(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_overlap_top","type":"checkout.session.completed","data":{"object":{"id":"cs_overlap","payment_intent":"pi_overlap","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    send({"id":"evt_overlap_open","type":"charge.dispute.created","data":{"object":{"id":"dp_overlap","payment_intent":"pi_overlap","amount":400}}})
    send({"id":"evt_overlap_refund","type":"charge.refunded","data":{"object":{"payment_intent":"pi_overlap","amount_refunded":1000}}})
    assert app.balance(uid)==-4_000_000
    send({"id":"evt_overlap_won","type":"charge.dispute.closed","data":{"object":{"id":"dp_overlap","payment_intent":"pi_overlap","amount":400,"status":"won"}}})
    assert app.balance(uid)==0

def test_out_of_order_won_dispute_restores_credit(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_early_dp","type":"charge.dispute.created","data":{"object":{"id":"dp_early","payment_intent":"pi_early","amount":400}}})
    send({"id":"evt_late_top","type":"checkout.session.completed","data":{"object":{"id":"cs_late_dp","payment_intent":"pi_early","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    assert app.balance(uid)==6_000_000
    send({"id":"evt_early_won","type":"charge.dispute.closed","data":{"object":{"id":"dp_early","payment_intent":"pi_early","amount":400,"status":"won"}}})
    assert app.balance(uid)==10_000_000

def test_dispute_won_before_checkout_never_debits(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_dp_first","type":"charge.dispute.created","data":{"object":{"id":"dp_pre_won","payment_intent":"pi_pre_won","amount":400}}})
    send({"id":"evt_won_first","type":"charge.dispute.closed","data":{"object":{"id":"dp_pre_won","payment_intent":"pi_pre_won","status":"won"}}})
    send({"id":"evt_checkout_last","type":"checkout.session.completed","data":{"object":{"id":"cs_pre_won","payment_intent":"pi_pre_won","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    assert app.balance(uid)==10_000_000

def test_refund_survives_won_dispute_when_both_precede_checkout(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_pre_ref","type":"charge.refunded","data":{"object":{"payment_intent":"pi_mix","amount_refunded":250}}})
    send({"id":"evt_pre_dp","type":"charge.dispute.created","data":{"object":{"id":"dp_mix","payment_intent":"pi_mix","amount":400}}})
    send({"id":"evt_mix_top","type":"checkout.session.completed","data":{"object":{"id":"cs_mix","payment_intent":"pi_mix","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    assert app.balance(uid)==3_500_000
    send({"id":"evt_mix_won","type":"charge.dispute.closed","data":{"object":{"id":"dp_mix","payment_intent":"pi_mix","status":"won"}}})
    assert app.balance(uid)==7_500_000

def test_won_closure_before_delayed_dispute_create_never_debits(client,monkeypatch):
    uid,_=make_user_key(); secret="whsec_test"; monkeypatch.setattr(app,"STRIPE_WEBHOOK_SECRET",secret)
    def send(event):
        raw=json.dumps(event,separators=(",",":")).encode(); ts=str(int(time.time())); sig=hmac.new(secret.encode(),ts.encode()+b"."+raw,hashlib.sha256).hexdigest()
        return client.post("/webhooks/stripe",content=raw,headers={"Stripe-Signature":f"t={ts},v1={sig}","Content-Type":"application/json"})
    send({"id":"evt_known_top","type":"checkout.session.completed","data":{"object":{"id":"cs_known","payment_intent":"pi_known","payment_status":"paid","metadata":{"user_id":str(uid),"credits_cents":"1000"}}}})
    send({"id":"evt_known_won","type":"charge.dispute.closed","data":{"object":{"id":"dp_delayed","payment_intent":"pi_known","status":"won"}}})
    send({"id":"evt_known_created","type":"charge.dispute.created","data":{"object":{"id":"dp_delayed","payment_intent":"pi_known","amount":400}}})
    assert app.balance(uid)==10_000_000

def test_stale_reservation_is_reconciled(client):
    uid,_=make_user_key(); old="2000-01-01T00:00:00+00:00"
    with app.dbconn() as db:
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,1_000_000,"seed","seed",old))
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,-500_000,"usage_reservation","stale",old))
        db.execute("INSERT INTO spend_reservations(ref,user_id,microusd,created_at) VALUES(?,?,?,?)",("stale",uid,500_000,old))
    assert app.balance(uid)==500_000
    assert app.reconcile_stale_reservations()==1
    assert app.balance(uid)==1_000_000
    assert app.reconcile_stale_reservations()==0

def test_failed_paid_provider_refunds_reservation(client):
    uid,key=make_user_key()
    with app.dbconn() as db: db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,1_000_000,"test","seed",app.now_iso()))
    payload={"model":"djev","state":"x","questions":{"q":{"type":"choice","criteria":{"a":None,"b":None}}}}
    r=client.post("/v1/systemone",headers={"Authorization":"Bearer "+key},json=payload)
    assert r.status_code==502
    assert app.balance(uid)==1_000_000

def test_meta_routes_by_published_score_and_returns_concrete_model(client,monkeypatch):
    uid,key=make_user_key()
    with app.dbconn() as db: db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,1_000_000,"test","seed",app.now_iso()))
    monkeypatch.setitem(app.PROVIDER_HEALTH,"djev",True)
    monkeypatch.setitem(app.PROVIDER_HEALTH,"semif-qwen3.5-4b",True)
    calls=[]
    async def fake(model,body,request):
        calls.append(model)
        if model=="classifier-fast": raise RuntimeError("temporary outage")
        return {"answers":{"q":{"type":"noul","noul":0.7}}}
    monkeypatch.setattr(app,"call_model",fake)
    payload={"model":"jev-class","state":"x","questions":{"q":{"type":"noul"}}}
    response=client.post("/v1/systemone",headers={"Authorization":"Bearer "+key},json=payload)
    assert response.status_code==200
    assert calls==["classifier-fast","semif-qwen3.5-4b"]
    assert response.headers["X-Jev-Model"]=="semif-qwen3.5-4b"
    assert response.json()["model"]=="semif-qwen3.5-4b"
    assert response.headers["X-Jev-Cost-Usd"]=="0.000050"

def test_image_meta_uses_separate_image_ranking(client,monkeypatch):
    uid,key=make_user_key()
    with app.dbconn() as db: db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,1_000_000,"test","seed",app.now_iso()))
    monkeypatch.setitem(app.PROVIDER_HEALTH,"djev",True)
    monkeypatch.setitem(app.PROVIDER_HEALTH,"decider-2b-vision",True)
    image="data:image/png;base64,"+base64.b64encode(b"tiny").decode()
    async def fake(model,body,request): return {"answers":{}}
    monkeypatch.setattr(app,"call_model",fake)
    payload={"model":"image-jev-class","state":"x","images":[image],"questions":{"q":{"type":"choice","criteria":{"a":None,"b":None}}}}
    response=client.post("/v1/multimodal",headers={"Authorization":"Bearer "+key},json=payload)
    assert response.status_code==200
    assert response.headers["X-Jev-Model"]=="decider-2b-vision"

def test_hosting_start_uses_provider_quote_and_charges_first_minute(client,monkeypatch):
    uid,_=make_user_key()
    with app.dbconn() as db: db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,1_000_000,"test","seed",app.now_iso()))
    async def control(method,path,payload=None):
        if path=="/quote": return {"quote_id":"q1","model":"djev-spark","provider":"lium","provider_microusd_per_minute":10_000,"expires_at":"2999-01-01T00:00:00+00:00"}
        assert method=="POST" and path=="/instances" and payload["quote_id"]=="q1"
        return {"id":"provider-1","provider":"lium","provider_microusd_per_minute":10_000,"endpoint":"https://private.invalid"}
    monkeypatch.setattr(app,"hosting_control",control)
    monkeypatch.setattr(app,"require_user",lambda request:{"id":uid,"email":"test@example.com"})
    monkeypatch.setattr(app,"check_csrf",lambda request,value:None)
    response=client.post("/hosting/start",data={"model":"djev-spark","csrf":"x"},follow_redirects=False)
    assert response.status_code==303
    price=11_000
    assert app.balance(uid)==1_000_000-price
    with app.dbconn() as db:
        instance=db.execute("SELECT * FROM hosting_instances").fetchone()
        assert instance["status"]=="running" and instance["provider_microusd_per_minute"]==10_000 and instance["billed_minutes"]==1
        assert db.execute("SELECT COUNT(*) n FROM hosting_minute_events").fetchone()["n"]==1
        assert db.execute("SELECT COUNT(*) n FROM usage_events WHERE provider='hosting:lium'").fetchone()["n"]==1

@pytest.mark.asyncio
async def test_reaper_charges_minutes_and_stops_at_zero(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"hosting.db")); app.ensure_db()
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("host@example.com","Host","host-sub",app.now_iso()))
        uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,15_000,"test","seed",app.now_iso()))
        old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,provider_instance_id,status,endpoint,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?,?)",("i1",uid,"djev-spark","lium","p1","x",12_467,11_334,1,old,old,app.now_iso()))
    deleted=[]
    async def control(method,path,payload=None):
        if method=="GET": return {"instances":[{"id":"p1","managed_by":"jev-router"},{"id":"orphan","managed_by":"jev-router"}]}
        deleted.append(path); return {}
    monkeypatch.setattr(app,"hosting_control",control)
    result=await app.reconcile_hosting()
    assert result["stopped"]==1 and result["orphans_removed"]==1
    assert "/instances/orphan" in deleted and "/instances/p1" in deleted

@pytest.mark.asyncio
async def test_reaper_removes_only_managed_orphans(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"orphans.db")); app.ensure_db(); deleted=[]
    async def control(method,path,payload=None):
        if method=="GET": return {"instances":[{"id":"ours","managed_by":"jev-router"},{"id":"other","managed_by":"someone-else"}]}
        deleted.append(path); return {}
    monkeypatch.setattr(app,"hosting_control",control)
    assert (await app.reconcile_hosting())["orphans_removed"]==1
    assert deleted==["/instances/ours"]

@pytest.mark.asyncio
async def test_reaper_does_not_delete_instance_persisted_during_provider_list(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"start-race.db")); app.ensure_db(); deleted=[]
    async def control(method,path,payload=None):
        if method=="GET":
            now=app.now_iso()
            with app.dbconn() as db:
                db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("race@example.com","Race","race",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
                db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,provider_instance_id,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)",("race",uid,"djev-spark","lium","new-pod",100,90,1,now,now,now))
            return {"instances":[{"id":"new-pod","managed_by":"jev-router"}]}
        deleted.append(path); return {}
    monkeypatch.setattr(app,"hosting_control",control)
    result=await app.reconcile_hosting()
    assert result["orphans_removed"]==0 and deleted==[]
    with app.dbconn() as db: assert db.execute("SELECT status FROM hosting_instances WHERE id='race'").fetchone()["status"]=="running"

@pytest.mark.asyncio
async def test_reaper_preserves_provider_instance_owned_by_unmapped_start(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"unmapped-start.db")); app.ensure_db(); now=app.now_iso(); deleted=[]
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("owner@example.com","Owner","owner",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,'starting',?,?,0,?,?,?)",("start-owner",uid,"djev-spark","lium",100,90,now,now,now))
    async def control(method,path,payload=None):
        if method=="GET": return {"instances":[{"id":"new-pod","managed_by":"jev-router","owner":"start-owner"}]}
        deleted.append(path); return {}
    monkeypatch.setattr(app,"hosting_control",control)
    result=await app.reconcile_hosting()
    assert result["orphans_removed"]==0 and deleted==[]
    with app.dbconn() as db:
        db.execute("UPDATE hosting_instances SET provider_instance_id=?,status='running' WHERE id=?",("new-pod","start-owner"))
        assert db.execute("SELECT provider_instance_id FROM hosting_instances WHERE id='start-owner'").fetchone()[0]=="new-pod"

@pytest.mark.asyncio
async def test_reaper_empty_verified_list_stops_missing_instance(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"missing.db")); app.ensure_db()
    now=app.now_iso()
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("m@example.com","M","m",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,provider_instance_id,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)",("i",uid,"djev-spark","lium","gone",100,90,1,now,now,now))
    async def control(method,path,payload=None): return {"instances":[]}
    monkeypatch.setattr(app,"hosting_control",control)
    result=await app.reconcile_hosting(); assert result["provider_verified"] and result["stopped"]==1
    with app.dbconn() as db: assert db.execute("SELECT status FROM hosting_instances WHERE id='i'").fetchone()["status"]=="stopped"

@pytest.mark.asyncio
async def test_reaper_provider_failure_neither_charges_nor_stops(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"verifyfail.db")); app.ensure_db()
    async def control(method,path,payload=None): raise RuntimeError("provider unavailable")
    monkeypatch.setattr(app,"hosting_control",control)
    result=await app.reconcile_hosting()
    assert result["provider_verified"] is False and result["charged_minutes"]==0

def test_hosting_runtime_respects_user_cap(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"cap.db")); app.ensure_db(); now=app.now_iso()
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("c@example.com","C","c",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,10000,"seed","seed",now))
        db.execute("INSERT INTO account_settings(user_id,daily_spend_cap_microusd) VALUES(?,?)",(uid,1500))
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,'running',?,?,0,?,?,?)",("cap",uid,"djev-spark","lium",1000,900,now,now,now))
    assert app.charge_hosting_minute("cap",1)
    assert app.charge_hosting_minute("cap",2) is False

def test_live_payments_cannot_be_enabled_by_flag():
    if app.STRIPE_MODE=="live": assert app.PAYMENTS_ENABLED is False

def test_hosting_rejects_quote_above_displayed_price(client,monkeypatch):
    uid,_=make_user_key()
    async def control(method,path,payload=None):
        return {"quote_id":"expensive","model":"djev-spark","provider":"lium","provider_microusd_per_minute":99_000,"expires_at":"2999-01-01T00:00:00+00:00"}
    monkeypatch.setattr(app,"hosting_control",control); monkeypatch.setattr(app,"require_user",lambda request:{"id":uid}); monkeypatch.setattr(app,"check_csrf",lambda request,value:None)
    response=client.post("/hosting/start",data={"model":"djev-spark"})
    assert response.status_code==409
    with app.dbconn() as db: assert db.execute("SELECT COUNT(*) n FROM hosting_instances").fetchone()["n"]==0

@pytest.mark.asyncio
async def test_failed_provider_delete_is_retried(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"retry.db")); app.ensure_db(); now=app.now_iso()
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("r@example.com","R","r",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,provider_instance_id,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)",("retry",uid,"djev-spark","lium","pod",100,90,0,now,now,now))
        row=db.execute("SELECT * FROM hosting_instances WHERE id='retry'").fetchone()
    attempts=0
    async def failing(method,path,payload=None):
        nonlocal attempts; attempts+=1; raise RuntimeError("temporary")
    monkeypatch.setattr(app,"hosting_control",failing)
    with pytest.raises(RuntimeError): await app.stop_hosting_instance(row,"user stop")
    with app.dbconn() as db: assert db.execute("SELECT status FROM hosting_instances WHERE id='retry'").fetchone()["status"]=="stopping"
    async def recovered(method,path,payload=None):
        if method=="GET": return {"instances":[{"id":"pod","managed_by":"jev-router"}]}
        return {}
    monkeypatch.setattr(app,"hosting_control",recovered)
    result=await app.reconcile_hosting(); assert result["stopped"]==1
    with app.dbconn() as db: assert db.execute("SELECT status FROM hosting_instances WHERE id='retry'").fetchone()["status"]=="stopped"

@pytest.mark.asyncio
async def test_stopping_instance_missing_from_verified_provider_is_finalized(tmp_path,monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"lost-response.db")); app.ensure_db(); now=app.now_iso()
    with app.dbconn() as db:
        db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",("lost@example.com","Lost","lost",now)); uid=db.execute("SELECT id FROM users").fetchone()[0]
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,provider_instance_id,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at,stop_reason) VALUES(?,?,?,?,?,'stopping',?,?,?,?,?,?,?)",("lost",uid,"djev-spark","lium","already-gone",100,90,0,now,now,now,"user stop"))
    async def verified_empty(method,path,payload=None):
        assert method=="GET" and path=="/instances"
        return {"instances":[]}
    monkeypatch.setattr(app,"hosting_control",verified_empty)
    result=await app.reconcile_hosting()
    assert result["provider_verified"] is True and result["stopped"]==1
    with app.dbconn() as db:
        row=db.execute("SELECT status,stopped_at FROM hosting_instances WHERE id='lost'").fetchone()
        assert row["status"]=="stopped" and row["stopped_at"]

def test_old_hosting_minute_schema_is_migrated(tmp_path,monkeypatch):
    path=tmp_path/"old-schema.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE hosting_minute_events(instance_id TEXT NOT NULL, minute_number INTEGER NOT NULL, user_id INTEGER NOT NULL, price_microusd INTEGER NOT NULL, provider_microusd INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(instance_id,minute_number))")
        db.execute("INSERT INTO hosting_minute_events VALUES(?,?,?,?,?,?)",("old",1,1,100,90,app.now_iso()))
    monkeypatch.setattr(app,"DB_PATH",str(path)); app.ensure_db()
    with sqlite3.connect(path) as db:
        columns={row[1] for row in db.execute("PRAGMA table_info(hosting_minute_events)")}
        assert "usage_event_id" in columns
        assert db.execute("SELECT COUNT(*) FROM hosting_minute_events WHERE instance_id='old'").fetchone()[0]==1
