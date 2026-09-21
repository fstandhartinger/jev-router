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
    assert "No default model" in data["neutrality"]
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
