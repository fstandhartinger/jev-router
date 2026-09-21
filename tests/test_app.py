import base64, hashlib, hmac, json, time
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
    assert next(x for x in data["data"] if x["id"]=="jev-typesafe")["status"]=="disabled"

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
