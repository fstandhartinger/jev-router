"""Jev Router product gateway.

Explicit routing is a product invariant: the server never chooses a model from a
benchmark score. Request bodies (including images) are never logged or stored.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

APP_URL = os.getenv("APP_URL", "http://localhost:8080").rstrip("/")
DB_PATH = os.getenv("DATABASE_PATH", "/data/jev-router.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-development-only-change-me")
STRIPE_SECRET_KEY = os.getenv("STRIPE_TEST_SECRET_KEY") or os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_TEST_WEBHOOK_SECRET") or os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_MODE = "test" if (STRIPE_SECRET_KEY.startswith("sk_test_") or not STRIPE_SECRET_KEY) else "live"
PAYMENTS_ENABLED = (
    os.getenv("PAYMENTS_ENABLED_TEST", "false").lower() == "true"
    if STRIPE_MODE == "test"
    else os.getenv("PAYMENTS_ENABLED_LIVE", "false").lower() == "true"
)
STRIPE_AUTOMATIC_TAX = os.getenv("STRIPE_AUTOMATIC_TAX", "false").lower() == "true"
MIN_TOPUP_CENTS = int(os.getenv("MIN_TOPUP_CENTS", "1000"))
OPERATOR_DAILY_CAP_CENTS = int(os.getenv("OPERATOR_DAILY_CAP_CENTS", "2500"))
MAX_BODY = 2_500_000
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2_000_000

MODELS: dict[str, dict[str, Any]] = {
    "classifier-fast": {"provider":"classifier.dev","status":"live","modalities":["text"],"price_per_1k_cents":0,"billing":"free","jevbench":{"track":"text-v1.2","score":84.8},"terms":"Free within published limits."},
    "jev-typesafe": {"provider":"TypeSafe","status":"disabled","modalities":["text"],"price_per_1k_cents":0,"billing":"direct provider use only; no router markup","jevbench":{"track":"text-v1.2","score":75.4},"terms":"Disabled pending written standalone-gateway permission; the original Jev does not accept images."},
    "jev-vercel": {"provider":"Vercel AI Gateway","status":"disabled","modalities":["text"],"price_per_1k_cents":0,"billing":"direct provider use only; no router markup","jevbench":{"track":"text-v1.2","score":75.4},"terms":"Disabled: Vercel does not override the underlying TypeSafe restriction."},
    "semif-qwen3.5-4b": {"provider":"self-hosted SemIf","status":"live" if os.getenv("SEMIF_ENDPOINT") else "offline","modalities":["text"],"price_per_1k_cents":5,"billing":"$0.05 / 1,000 decisions (includes disclosed infrastructure margin)","jevbench":{"track":"text-v1.2","score":74.7},"terms":"Open implementation; scale-to-zero endpoint."},
    "djev": {"provider":"self-hosted djev-dev","status":"live" if os.getenv("DJEV_ENDPOINT") else "offline","modalities":["text","image"],"price_per_1k_cents":8,"billing":"$0.08 / 1,000 decisions (includes disclosed infrastructure margin)","jevbench":{"track":"text-v1.2","score":74.3},"multimodal_benchmark":{"track":"public-pilot-80","score":53.75},"terms":"Apache-2.0 runtime and weights; Maisa hosted API is not proxied."},
    "decider-2b-vision": {"provider":"self-hosted Mapika decider","status":"live" if os.getenv("DECIDER_ENDPOINT") else "offline","modalities":["text","image"],"price_per_1k_cents":5,"billing":"$0.05 / 1,000 decisions (includes disclosed infrastructure margin)","jevbench":None,"multimodal_benchmark":{"track":"public-pilot-80","score":56.25},"terms":"Self-hosted image decision model."},
    "laya-421m": {"provider":"self-hosted Laya","status":"live" if os.getenv("LAYA_ENDPOINT") else "offline","modalities":["text"],"price_per_1k_cents":1,"billing":"$0.01 / 1,000 decisions (includes disclosed infrastructure margin)","jevbench":None,"terms":"CPU-capable; endpoint remains off until latency is validated."},
}
if STRIPE_MODE == "test" and PAYMENTS_ENABLED:
    MODELS["stripe-test-paid"] = {
        "provider":"Jev Router test fixture",
        "status":"live",
        "modalities":["text"],
        "price_per_1k_cents":10,
        "billing":"$0.10 / 1,000 decisions; Stripe test mode only",
        "jevbench":None,
        "terms":"Deterministic non-production route for end-to-end billing acceptance tests.",
    }

app = FastAPI(title="Jev Router", docs_url=None, redoc_url=None, openapi_url="/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=APP_URL.startswith("https://"), max_age=86400 * 14)

@app.middleware("http")
async def canonical_host(request: Request, call_next):
    if APP_URL.startswith("https://") and request.url.hostname in {
        "www.jev-router.com",
        "jev-router.app.mintapis.com",
    }:
        target = APP_URL + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=308)
    return await call_next(request)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"strict-origin-when-cross-origin","Permissions-Policy":"camera=(), microphone=(), geolocation=()","Content-Security-Policy":"default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; base-uri 'self'"})
    return response

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_db() -> None:
    if DATABASE_URL:
        import psycopg
        ddl="""
        CREATE TABLE IF NOT EXISTS users(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, google_sub TEXT UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_keys(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, user_id BIGINT NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
        CREATE TABLE IF NOT EXISTS credit_events(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, user_id BIGINT NOT NULL, microusd BIGINT NOT NULL, kind TEXT NOT NULL, ref TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_events(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, user_id BIGINT, api_key_id BIGINT, model TEXT NOT NULL, microusd BIGINT NOT NULL, provider TEXT NOT NULL, latency_ms DOUBLE PRECISION NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_events(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_payments(payment_intent TEXT PRIMARY KEY, user_id BIGINT NOT NULL, credited_microusd BIGINT NOT NULL, refunded_microusd BIGINT NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_checkout_intents(session_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, amount_cents BIGINT NOT NULL, currency TEXT NOT NULL, receipt_email TEXT NOT NULL, status TEXT NOT NULL, payment_intent TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending(id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, microusd BIGINT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_settings(user_id BIGINT PRIMARY KEY, daily_spend_cap_microusd BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS spend_reservations(ref TEXT PRIMARY KEY, user_id BIGINT NOT NULL, microusd BIGINT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_windows(api_key_id BIGINT NOT NULL, bucket_window TEXT NOT NULL, requests BIGINT NOT NULL, PRIMARY KEY(api_key_id,bucket_window));
        CREATE TABLE IF NOT EXISTS stripe_disputes(dispute_id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, user_id BIGINT NOT NULL, microusd BIGINT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending_disputes(dispute_id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, microusd BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending_dispute_closures(dispute_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_refund_totals(payment_intent TEXT PRIMARY KEY, microusd BIGINT NOT NULL);
        """
        with psycopg.connect(DATABASE_URL) as db:
            for statement in ddl.split(";"):
                if statement.strip(): db.execute(statement)
    else:
        p = Path(DB_PATH); p.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as db:
            db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, google_sub TEXT UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_keys(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
        CREATE TABLE IF NOT EXISTS credit_events(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, microusd INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_events(id INTEGER PRIMARY KEY, user_id INTEGER, api_key_id INTEGER, model TEXT NOT NULL, microusd INTEGER NOT NULL, provider TEXT NOT NULL, latency_ms REAL NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_events(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_payments(payment_intent TEXT PRIMARY KEY, user_id INTEGER NOT NULL, credited_microusd INTEGER NOT NULL, refunded_microusd INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_checkout_intents(session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, receipt_email TEXT NOT NULL, status TEXT NOT NULL, payment_intent TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending(id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, microusd INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_settings(user_id INTEGER PRIMARY KEY, daily_spend_cap_microusd INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS spend_reservations(ref TEXT PRIMARY KEY, user_id INTEGER NOT NULL, microusd INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_windows(api_key_id INTEGER NOT NULL, bucket_window TEXT NOT NULL, requests INTEGER NOT NULL, PRIMARY KEY(api_key_id,bucket_window));
        CREATE TABLE IF NOT EXISTS stripe_disputes(dispute_id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, user_id INTEGER NOT NULL, microusd INTEGER NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending_disputes(dispute_id TEXT PRIMARY KEY, payment_intent TEXT NOT NULL, microusd INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_pending_dispute_closures(dispute_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stripe_refund_totals(payment_intent TEXT PRIMARY KEY, microusd INTEGER NOT NULL);
        """)

class PgCompat:
    def __init__(self,conn): self.conn=conn
    def execute(self,query,params=()): return self.conn.execute(query.replace("?","%s"),params)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

@contextmanager
def dbconn():
    ensure_db()
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        db = PgCompat(psycopg.connect(DATABASE_URL,row_factory=dict_row))
    else:
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
    try:
        yield db; db.commit()
    finally: db.close()

def balance(user_id: int) -> int:
    with dbconn() as db:
        return int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM credit_events WHERE user_id=?", (user_id,)).fetchone()["total"])

def csrf(request: Request) -> str:
    if not request.session.get("csrf"): request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]

def check_csrf(request: Request, value: Any) -> None:
    if not value or not hmac.compare_digest(str(value), request.session.get("csrf", "")): raise HTTPException(403, "Invalid CSRF token")

def hash_key(value: str) -> str:
    return hashlib.sha256((SESSION_SECRET + value).encode()).hexdigest()

def current_user(request: Request):
    uid = request.session.get("user_id")
    if not uid: return None
    with dbconn() as db: return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def require_user(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401, "Sign in required")
    return user

def api_user(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer jvr_"): raise HTTPException(401, "A Jev Router API key is required")
    value = authorization.removeprefix("Bearer ").strip()
    with dbconn() as db:
        row = db.execute("SELECT k.id key_id,u.* FROM api_keys k JOIN users u ON u.id=k.user_id WHERE k.key_hash=? AND k.revoked_at IS NULL", (hash_key(value),)).fetchone()
    if not row: raise HTTPException(401, "Invalid or revoked API key")
    return row

def enforce_rate_limit(key_id: int) -> None:
    window=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    with dbconn() as db:
        db.execute("SELECT pg_advisory_xact_lock(?)",(key_id,)) if DATABASE_URL else db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT requests FROM rate_windows WHERE api_key_id=? AND bucket_window=?",(key_id,window)).fetchone()
        if row and int(row["requests"])>=120: raise HTTPException(429,"API key rate limit exceeded",headers={"Retry-After":"60"})
        db.execute("INSERT INTO rate_windows(api_key_id,bucket_window,requests) VALUES(?,?,1) ON CONFLICT(api_key_id,bucket_window) DO UPDATE SET requests=rate_windows.requests+1",(key_id,window))

def esc(value: Any) -> str:
    import html
    return html.escape(str(value))

CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--panel:#fff;--text:#171717;--muted:#666;--line:#ddd;--accent:#5d5fef;--soft:#f3f3f7}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}nav,main,footer{max-width:1120px;margin:auto;padding:20px 28px}nav{display:flex;align-items:center;gap:24px;border-bottom:1px solid var(--line)}nav .brand{font-weight:750;font-size:18px;margin-right:auto;color:var(--text)}a{color:inherit;text-decoration:none}nav a:not(.brand),.muted{color:var(--muted)}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:3px}.skip{position:absolute;left:-9999px}.skip:focus{left:12px;top:12px;background:var(--panel);padding:10px;z-index:10}.hero{padding:92px 0 68px;max-width:780px}.eyebrow{font-size:13px;color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.08em}h1{font-size:clamp(42px,7vw,72px);line-height:1.02;letter-spacing:-.055em;margin:16px 0 24px}h2{font-size:30px;letter-spacing:-.03em;margin-top:48px}h3{margin:0 0 8px}.lead{font-size:20px;color:var(--muted);max-width:700px}.actions{display:flex;gap:12px;margin-top:30px;flex-wrap:wrap}.button,button{display:inline-flex;border:1px solid var(--line);background:var(--panel);padding:10px 16px;border-radius:8px;font:inherit;cursor:pointer}.primary{background:var(--text);color:var(--bg);border-color:var(--text)}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px}.badge{display:inline-block;padding:3px 8px;border-radius:99px;background:var(--soft);font-size:12px}.live{color:#087a45}.offline{color:#946200}.byok{color:#5d5fef}code,pre{font:13px/1.5 ui-monospace,SFMono-Regular,monospace}pre{padding:18px;background:#111218;color:#e8e8ec;border-radius:10px;overflow:auto}.notice{border-left:3px solid var(--accent);padding:10px 16px;background:var(--soft)}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:11px;border-bottom:1px solid var(--line);vertical-align:top}input{width:100%;max-width:420px;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text)}footer{color:var(--muted);margin-top:70px;border-top:1px solid var(--line);display:flex;gap:18px;flex-wrap:wrap}@media(max-width:760px){nav{gap:12px;overflow:auto}nav a:not(.brand){white-space:nowrap}.hero{padding:55px 0}.grid{grid-template-columns:1fr}main{padding:16px}h1{font-size:44px}}@media(prefers-color-scheme:dark){:root{--bg:#0e0f12;--panel:#15161a;--text:#f2f2f3;--muted:#a0a0a7;--line:#292a30;--accent:#8b8dff;--soft:#1d1e24}}
"""

def page(title: str, body: str, user=None) -> HTMLResponse:
    auth = '<a href="/dashboard">Dashboard</a><a href="/logout">Sign out</a>' if user else '<a href="/login">Sign in</a>'
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Neutral infrastructure for explicit Jev-class decision model routing."><title>{esc(title)} · Jev Router</title><style>{CSS}</style></head><body><a class="skip" href="#main">Skip to content</a><nav aria-label="Main navigation"><a class="brand" href="/">Jev Router</a><a href="/models-page">Models</a><a href="/docs">Docs</a><a href="/status">Status</a>{auth}</nav><main id="main">{body}</main><footer><span>© 2026 productivity-boost.com Betriebs UG &amp; Co. KG</span><a href="/terms">Terms</a><a href="/privacy">Privacy</a><a href="/refunds">Refunds</a><a href="/impressum">Impressum</a></footer></body></html>''')

@app.on_event("startup")
def startup():
    ensure_db(); reconcile_stale_reservations()

@app.get("/health")
def health(): return {"ok": True, "stripe_mode": STRIPE_MODE, "payments_enabled": PAYMENTS_ENABLED}

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user=current_user(request)
    return page("Decision-model infrastructure", '''<section class="hero"><div class="eyebrow">Decision infrastructure, without hidden routing</div><h1>One API for Jev-class decision models.</h1><p class="lead">Choose the model yourself, or provide an ordered fallback list. Text and image decisions use the same typed answer contract. Benchmarks inform; they never route.</p><div class="actions"><a class="button primary" href="/docs">Read the API docs</a><a class="button" href="/models-page">Compare models</a></div></section><section class="grid"><div class="card"><h3>Explicit by design</h3><p class="muted">No default model and no ranking-based selection. Every response names the provider and model.</p></div><div class="card"><h3>Neutral economics</h3><p class="muted">Self-hosted prices disclose a small infrastructure margin. Third-party routes remain disabled until written permission exists.</p></div><div class="card"><h3>Privacy bounded</h3><p class="muted">We do not log or retain request bodies. Your selected provider receives the request.</p></div></section>''', user)

@app.get("/models")
def models(): return {"object":"list","data":[{"id":k,**v} for k,v in MODELS.items()],"neutrality":"No default model. Benchmark fields are informational and never used for routing."}

@app.get("/models-page", response_class=HTMLResponse)
def models_page(request: Request):
    def bench(v):
        parts=[]
        if v.get("jevbench"): parts.append(f'{v["jevbench"]["score"]} ({v["jevbench"]["track"]})')
        if v.get("multimodal_benchmark"): parts.append(f'{v["multimodal_benchmark"]["score"]}% ({v["multimodal_benchmark"]["track"]})')
        return "<br>".join(map(esc,parts)) or "—"
    rows="".join(f'<tr><th scope="row"><strong>{esc(k)}</strong><br><span class="muted">{esc(v["provider"])}</span></th><td><span class="badge {v["status"]}">{esc(v["status"])}</span></td><td>{esc(", ".join(v["modalities"]))}</td><td>{esc(v["billing"])}</td><td>{bench(v)}</td></tr>' for k,v in MODELS.items())
    return page("Models", '<h1 style="font-size:52px">Models</h1><p class="lead">Availability, capabilities, prices, and separately labeled benchmark context. Scores never choose a route.</p><div class="table-wrap"><table><thead><tr><th>Model</th><th>Status</th><th>Input</th><th>Price</th><th>Benchmark track</th></tr></thead><tbody>'+rows+'</tbody></table></div><p class="notice">TypeSafe and Vercel Jev are disabled pending written permission. Hosted Maisa djev is not proxied. Benchmarks were observed 21 September 2026.</p>', current_user(request))

DOC_EXAMPLE='''curl https://jev-router.com/v1/systemone \\
  -H "Authorization: Bearer jvr_…" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"classifier-fast","state":"Mia owns a red bicycle.","questions":{"color":{"type":"choice","instructions":"What color?","criteria":{"red":null,"blue":null}}}}' '''

@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    return page("API docs", f'''<h1 style="font-size:52px">API docs</h1><p class="lead">The TypeSafe System One contract, with explicit routing.</p><h2>Text decisions</h2><pre>{esc(DOC_EXAMPLE)}</pre><h2>Multimodal decisions</h2><p>POST <code>/v1/multimodal</code> with the same body plus up to {MAX_IMAGES} <code>data:image/png;base64,…</code> values in <code>images</code>. Only image-capable models are accepted.</p><h2>Fallbacks</h2><pre>{esc(json.dumps({"model":"djev","fallback":["decider-2b-vision"],"state":"…","images":["data:image/webp;base64,…"],"questions":{"decision":{"type":"choice","criteria":{"a":None,"b":None}}}}, indent=2))}</pre><h2>Python</h2><pre>import requests\nrequests.post("https://jev-router.com/v1/systemone", headers={{"Authorization":"Bearer jvr_…"}}, json={{...}}).json()</pre><h2>JavaScript</h2><pre>await fetch("https://jev-router.com/v1/systemone", {{method:"POST", headers:{{Authorization:"Bearer jvr_…","Content-Type":"application/json"}}, body:JSON.stringify(payload)}})</pre><h2>OpenAI-compatible convenience</h2><p><code>POST /v1/chat/completions</code> accepts <code>model</code> and a final user message containing a JSON System One request. It is a convenience adapter; the native route is canonical.</p>''', current_user(request))

LEGAL={
"terms":("Terms of service","Jev Router is a prepaid, usage-based decision-model gateway. You must choose a model explicitly and use it lawfully. Disabled third-party routes carry no Jev Router markup if later permitted. Self-hosted usage is deducted from prepaid credits at the published price. Checkout shows any Stripe-calculated tax before payment when Stripe Tax is enabled; we make no statement about your tax treatment. No SLA or warranty is provided. We may suspend abusive or unsafe traffic and enforce rate and spend limits."),
"privacy":("Privacy","We store your Google account identifier, email, API-key hashes, payment references, credit ledger, and aggregate usage metadata. We do not store request bodies or images. The provider you explicitly select receives request content. Images can contain personal data and metadata; remove anything you do not intend to send. Stripe processes payments and receipts; Google handles sign-in. Account and billing records are retained as legally required. Contact: florian.standhartinger@gmail.com."),
"refunds":("Refund policy","Unused prepaid credits may be refunded on request within 14 days of purchase where legally required. Consumed credits are non-refundable because the compute service has already been delivered. Chargebacks or duplicate payments are reviewed individually. Contact us from the purchasing email address."),
"impressum":("Impressum","productivity-boost.com Betriebs UG (haftungsbeschränkt) &amp; Co. KG<br>Passau, Germany<br>VAT ID: DE296812612<br>Responsible: Florian Standhartinger<br>Email: florian.standhartinger@gmail.com")}

for _slug,(_title,_text) in LEGAL.items():
    def make_legal(slug=_slug,title=_title,text=_text):
        @app.get("/"+slug, response_class=HTMLResponse, name="legal_"+slug)
        def legal(request: Request): return page(title, f'<h1 style="font-size:52px">{title}</h1><div class="card"><p>{text}</p><p class="muted">Last updated 21 September 2026.</p></div>', current_user(request))
    make_legal()

@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    live=sum(v["status"]=="live" for v in MODELS.values())
    return page("Status", f'<h1 style="font-size:52px">Status</h1><div class="card"><h3><span class="live">●</span> Gateway operational</h3><p class="muted">{live} routes currently report live. Scale-to-zero models may have a cold start. Check machine-readable state at <a href="/models"><code>/models</code></a>.</p></div>', current_user(request))

@app.get("/login")
def login(request: Request):
    if not os.getenv("GOOGLE_CLIENT_ID"): raise HTTPException(503,"Google sign-in is not configured")
    state=secrets.token_urlsafe(24); request.session["oauth_state"]=state
    query=urllib.parse.urlencode({"client_id":os.environ["GOOGLE_CLIENT_ID"],"redirect_uri":APP_URL+"/auth/google/callback","response_type":"code","scope":"openid email profile","state":state,"prompt":"select_account"})
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?"+query)

@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str, state: str):
    if not hmac.compare_digest(state, request.session.pop("oauth_state", "")): raise HTTPException(400,"Invalid OAuth state")
    async with httpx.AsyncClient(timeout=15) as client:
        token=(await client.post("https://oauth2.googleapis.com/token",data={"code":code,"client_id":os.environ["GOOGLE_CLIENT_ID"],"client_secret":os.environ["GOOGLE_CLIENT_SECRET"],"redirect_uri":APP_URL+"/auth/google/callback","grant_type":"authorization_code"})).json()
        info=(await client.get("https://openidconnect.googleapis.com/v1/userinfo",headers={"Authorization":"Bearer "+token["access_token"]})).json()
    if not info.get("email_verified"): raise HTTPException(403,"Verified Google email required")
    with dbconn() as db:
        existing=db.execute("SELECT id FROM users WHERE google_sub=?",(info["sub"],)).fetchone()
        if existing:
            uid=existing["id"]; db.execute("UPDATE users SET email=?,name=? WHERE id=?",(info["email"],info.get("name"),uid))
        else:
            if db.execute("SELECT 1 FROM users WHERE email=?",(info["email"],)).fetchone(): raise HTTPException(409,"An account already exists for this email; contact support")
            if DATABASE_URL:
                uid=db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?) RETURNING id",(info["email"],info.get("name"),info["sub"],now_iso())).fetchone()["id"]
            else:
                db.execute("INSERT INTO users(email,name,google_sub,created_at) VALUES(?,?,?,?)",(info["email"],info.get("name"),info["sub"],now_iso()))
                uid=db.execute("SELECT id FROM users WHERE google_sub=?",(info["sub"],)).fetchone()["id"]
    request.session["user_id"]=uid
    return RedirectResponse("/dashboard",303)

@app.get("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/",303)

@app.get("/dashboard",response_class=HTMLResponse)
def dashboard(request: Request):
    u=require_user(request)
    with dbconn() as db:
        keys=db.execute("SELECT id,prefix,name,created_at,revoked_at FROM api_keys WHERE user_id=? ORDER BY id DESC",(u["id"],)).fetchall()
        usage=db.execute("SELECT model,COUNT(*) n,SUM(microusd) microusd FROM usage_events WHERE user_id=? GROUP BY model",(u["id"],)).fetchall()
        settings=db.execute("SELECT daily_spend_cap_microusd FROM account_settings WHERE user_id=?",(u["id"],)).fetchone()
    cap=settings["daily_spend_cap_microusd"] if settings else 100_000_000
    token=csrf(request)
    checkout_nonce=secrets.token_urlsafe(18); request.session["checkout_nonce"]=checkout_nonce
    keyrows="".join(f'<tr><td>{esc(k["name"])}</td><td><code>{esc(k["prefix"])}…</code></td><td>' + ('revoked' if k["revoked_at"] else f'<form method="post" action="/api-keys/{k["id"]}/revoke"><input type="hidden" name="csrf" value="{token}"><button>Revoke</button></form>') + '</td></tr>' for k in keys) or '<tr><td colspan=3 class="muted">No API keys yet.</td></tr>'
    usagerows="".join(f'<tr><td>{esc(x["model"])}</td><td>{x["n"]}</td><td>${x["microusd"]/1_000_000:.6f}</td></tr>' for x in usage) or '<tr><td colspan=3 class="muted">No usage yet.</td></tr>'
    return page("Dashboard",f'''<h1 style="font-size:52px">Dashboard</h1><div class="grid"><div class="card"><span class="muted">Credit balance</span><h2>${balance(u["id"])/1_000_000:.2f}</h2><form method="post" action="/billing/checkout"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="checkout_nonce" value="{checkout_nonce}"><label>Top up (USD cents, minimum {MIN_TOPUP_CENTS})<input name="amount_cents" type="number" min="{MIN_TOPUP_CENTS}" value="{MIN_TOPUP_CENTS}"></label><button class="primary" type="submit">Checkout in Stripe {STRIPE_MODE} mode</button></form><p class="muted">Stripe sends the receipt. Checkout displays Stripe-calculated tax when configured; no tax treatment is stated here.</p></div><div class="card"><h3>New API key</h3><form method="post" action="/api-keys"><input type="hidden" name="csrf" value="{token}"><label>Name<input name="name" maxlength="60" value="Default"></label><button type="submit">Create key</button></form><p class="muted">The full key is shown once. Stored keys are hashed and revocable.</p></div><div class="card"><h3>Account</h3><p>{esc(u["email"])}</p><form method="post" action="/settings/spend-cap"><input type="hidden" name="csrf" value="{token}"><label>Daily self-hosted spend cap (USD)<input name="cap_usd" type="number" min="0" max="1000" step="0.01" value="{cap/1_000_000:.2f}"></label><button>Save cap</button></form><a href="/usage">Detailed usage →</a></div></div><h2>API keys</h2><div class="table-wrap"><table><tr><th>Name</th><th>Key</th><th>Status</th></tr>{keyrows}</table></div><h2>Usage</h2><div class="table-wrap"><table><tr><th>Model</th><th>Decisions</th><th>Charged</th></tr>{usagerows}</table></div>''',u)

@app.post("/settings/spend-cap")
async def set_spend_cap(request:Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf"))
    try: value=round(float(form.get("cap_usd",0))*1_000_000)
    except Exception: raise HTTPException(400,"Invalid spend cap")
    if value<0 or value>1_000_000_000: raise HTTPException(400,"Spend cap must be between $0 and $1,000")
    with dbconn() as db: db.execute("INSERT INTO account_settings(user_id,daily_spend_cap_microusd) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET daily_spend_cap_microusd=excluded.daily_spend_cap_microusd",(u["id"],value))
    return RedirectResponse("/dashboard",303)

@app.post("/api-keys",response_class=HTMLResponse)
async def create_key(request: Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf")); name=str(form.get("name","Default"))[:60]
    raw="jvr_"+secrets.token_urlsafe(32); prefix=raw[:12]
    with dbconn() as db: db.execute("INSERT INTO api_keys(user_id,prefix,key_hash,name,created_at) VALUES(?,?,?,?,?)",(u["id"],prefix,hash_key(raw),name,now_iso()))
    return page("API key created",f'<h1 style="font-size:52px">Copy your key</h1><p class="notice">This is the only time the full key is shown.</p><pre id="new-key">{esc(raw)}</pre><a class="button" href="/dashboard">Done</a>',u)

@app.post("/api-keys/{key_id}/revoke")
async def revoke_key(key_id:int,request:Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf"))
    with dbconn() as db: db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",(now_iso(),key_id,u["id"]))
    return RedirectResponse("/dashboard",303)

@app.get("/usage",response_class=HTMLResponse)
def usage_page(request:Request):
    u=require_user(request)
    with dbconn() as db: rows=db.execute("SELECT created_at,model,provider,microusd,latency_ms FROM usage_events WHERE user_id=? ORDER BY id DESC LIMIT 200",(u["id"],)).fetchall()
    body="".join(f'<tr><td>{esc(r["created_at"][:19])}</td><td>{esc(r["model"])}</td><td>{esc(r["provider"])}</td><td>${r["microusd"]/1_000_000:.6f}</td><td>{r["latency_ms"]:.0f} ms</td></tr>' for r in rows) or '<tr><td colspan=5>No usage yet.</td></tr>'
    return page("Usage",f'<h1 style="font-size:52px">Usage</h1><p class="lead">Balance: ${balance(u["id"])/1_000_000:.2f}</p><div class="table-wrap"><table><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Model</th><th scope="col">Provider</th><th scope="col">Charged</th><th scope="col">Latency</th></tr></thead><tbody>{body}</tbody></table></div>',u)

@app.post("/billing/checkout")
async def checkout(request:Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf")); amount=int(form.get("amount_cents",0))
    nonce=str(form.get("checkout_nonce", ""))
    if not nonce or not hmac.compare_digest(nonce,request.session.get("checkout_nonce","")): raise HTTPException(409,"Stale checkout form; reload the dashboard")
    if amount<MIN_TOPUP_CENTS or amount>100_000: raise HTTPException(400,f"Top-up must be {MIN_TOPUP_CENTS}–100000 cents")
    if not PAYMENTS_ENABLED: raise HTTPException(503,f"Payments are disabled in Stripe {STRIPE_MODE} mode")
    if not STRIPE_SECRET_KEY: raise HTTPException(503,"Stripe test mode is not configured")
    data=[("mode","payment"),("success_url",APP_URL+"/dashboard?payment=success"),("cancel_url",APP_URL+"/dashboard?payment=cancelled"),("customer_email",u["email"]),("client_reference_id",str(u["id"])),("metadata[user_id]",str(u["id"])),("metadata[credits_cents]",str(amount)),("line_items[0][price_data][currency]","usd"),("line_items[0][price_data][unit_amount]",str(amount)),("line_items[0][price_data][product_data][name]","Jev Router prepaid credits"),("line_items[0][quantity]","1"),("payment_intent_data[receipt_email]",u["email"]),("automatic_tax[enabled]","true" if STRIPE_AUTOMATIC_TAX else "false")]
    async with httpx.AsyncClient(timeout=20) as client: resp=await client.post("https://api.stripe.com/v1/checkout/sessions",data=data,auth=(STRIPE_SECRET_KEY,""),headers={"Idempotency-Key":f"topup-{u['id']}-{amount}-{nonce}"})
    if resp.status_code>=400: raise HTTPException(502,"Stripe could not create a checkout session")
    session=resp.json()
    with dbconn() as db:
        db.execute("INSERT INTO stripe_checkout_intents(session_id,user_id,amount_cents,currency,receipt_email,status,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING",(session["id"],u["id"],amount,"usd",u["email"],"created",now_iso()))
    return RedirectResponse(session["url"],303)

def stripe_signature_valid(payload:bytes,header:str)->bool:
    if not STRIPE_WEBHOOK_SECRET:return False
    try:
        parts=dict(x.split("=",1) for x in header.split(",") if "=" in x); ts=parts.get("t",""); sigs=[x.split("=",1)[1] for x in header.split(",") if x.startswith("v1=")]
        if not ts or abs(time.time()-int(ts))>300:return False
    except (TypeError,ValueError): return False
    expected=hmac.new(STRIPE_WEBHOOK_SECRET.encode(),ts.encode()+b"."+payload,hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected,s) for s in sigs)

@app.post("/webhooks/stripe")
async def stripe_webhook(request:Request,stripe_signature:str|None=Header(None)):
    payload=await request.body()
    if not stripe_signature or not stripe_signature_valid(payload,stripe_signature): raise HTTPException(400,"Invalid Stripe signature")
    try: event=json.loads(payload); obj=event.get("data",{}).get("object",{})
    except Exception: raise HTTPException(400,"Invalid Stripe payload")
    if not event.get("id") or not event.get("type"): raise HTTPException(400,"Invalid Stripe event")
    with dbconn() as db:
        if db.execute("SELECT 1 FROM stripe_events WHERE id=?",(event["id"],)).fetchone(): return {"received":True,"duplicate":True}
        if event["type"]=="checkout.session.completed" and obj.get("payment_status")=="paid":
            uid=int(obj["metadata"]["user_id"]); cents=int(obj["metadata"]["credits_cents"])
            if PAYMENTS_ENABLED:
                expected_livemode=STRIPE_MODE=="live"
                intent=db.execute("SELECT * FROM stripe_checkout_intents WHERE session_id=?",(obj.get("id"),)).fetchone()
                valid=(
                    intent and bool(obj.get("livemode"))==expected_livemode
                    and obj.get("currency")==intent["currency"]
                    and int(obj.get("amount_total") or 0)==int(intent["amount_cents"])
                    and cents==int(intent["amount_cents"])
                    and uid==int(intent["user_id"])
                    and str(obj.get("client_reference_id"))==str(intent["user_id"])
                )
                if not valid: raise HTTPException(400,"Stripe checkout does not match a pending top-up")
            db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,cents*10_000,"stripe_topup",obj["id"],now_iso()))
            if PAYMENTS_ENABLED:
                db.execute("UPDATE stripe_checkout_intents SET status='paid',payment_intent=? WHERE session_id=?",(obj.get("payment_intent"),obj["id"]))
            if obj.get("payment_intent"):
                pi=obj["payment_intent"]; db.execute("INSERT INTO stripe_payments(payment_intent,user_id,credited_microusd,refunded_microusd,created_at) VALUES(?,?,?,?,?) ON CONFLICT(payment_intent) DO NOTHING",(pi,uid,cents*10_000,0,now_iso()))
                credited=cents*10_000
                # Refund notifications are cumulative; disputes are separate holds.
                refund_pending=int(db.execute("SELECT COALESCE(MAX(microusd),0) total FROM stripe_pending WHERE payment_intent=? AND kind='charge.refunded'",(pi,)).fetchone()["total"])
                refund_debit=min(credited,refund_pending); remaining=credited-refund_debit; dispute_debit=0
                if refund_pending: db.execute("INSERT INTO stripe_refund_totals(payment_intent,microusd) VALUES(?,?) ON CONFLICT(payment_intent) DO UPDATE SET microusd=excluded.microusd",(pi,refund_pending))
                for dispute in db.execute("SELECT * FROM stripe_pending_disputes WHERE payment_intent=?",(pi,)).fetchall():
                    closed=db.execute("SELECT status FROM stripe_pending_dispute_closures WHERE dispute_id=?",(dispute["dispute_id"],)).fetchone()
                    status="won" if closed and closed["status"]=="won" else "open"
                    allocated=0 if status=="won" else min(remaining,dispute["microusd"]); remaining-=allocated; dispute_debit+=allocated
                    db.execute("INSERT INTO stripe_disputes(dispute_id,payment_intent,user_id,microusd,status) VALUES(?,?,?,?,?) ON CONFLICT(dispute_id) DO NOTHING",(dispute["dispute_id"],pi,uid,allocated,status))
                    db.execute("DELETE FROM stripe_pending_dispute_closures WHERE dispute_id=?",(dispute["dispute_id"],))
                if refund_debit:
                    db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,-refund_debit,"stripe_pending_refund","pending-refund-"+pi,now_iso()))
                    db.execute("UPDATE stripe_payments SET refunded_microusd=? WHERE payment_intent=?",(refund_debit,pi))
                if dispute_debit:
                    db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,-dispute_debit,"stripe_pending_dispute","pending-dispute-"+pi,now_iso()))
                db.execute("DELETE FROM stripe_pending_disputes WHERE payment_intent=?",(pi,))
                db.execute("DELETE FROM stripe_pending WHERE payment_intent=?",(pi,))
        if event["type"] in ("charge.refunded","charge.dispute.created"):
            payment_intent=obj.get("payment_intent")
            payment=db.execute("SELECT * FROM stripe_payments WHERE payment_intent=?",(payment_intent,)).fetchone() if payment_intent else None
            target_amount=int(obj.get("amount_refunded") or obj.get("amount") or 0)*10_000
            if payment:
                if event["type"]=="charge.refunded":
                    previous=db.execute("SELECT microusd FROM stripe_refund_totals WHERE payment_intent=?",(payment_intent,)).fetchone(); previous=int(previous["microusd"]) if previous else 0
                    delta=min(max(0,target_amount-previous),max(0,payment["credited_microusd"]-previous))
                    db.execute("INSERT INTO stripe_refund_totals(payment_intent,microusd) VALUES(?,?) ON CONFLICT(payment_intent) DO UPDATE SET microusd=excluded.microusd",(payment_intent,max(previous,target_amount)))
                else:
                    dispute_id=obj.get("id",event["id"]); closed=db.execute("SELECT status FROM stripe_pending_dispute_closures WHERE dispute_id=?",(dispute_id,)).fetchone()
                    if closed and closed["status"]=="won":
                        delta=0
                        db.execute("INSERT INTO stripe_disputes(dispute_id,payment_intent,user_id,microusd,status) VALUES(?,?,?,?,?) ON CONFLICT(dispute_id) DO NOTHING",(dispute_id,payment_intent,payment["user_id"],0,"won"))
                        db.execute("DELETE FROM stripe_pending_dispute_closures WHERE dispute_id=?",(dispute_id,))
                    else:
                        already_held=int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM stripe_disputes WHERE payment_intent=? AND status='open'",(payment_intent,)).fetchone()["total"])
                        delta=min(target_amount,max(0,payment["credited_microusd"]-already_held))
                if delta:
                    db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(payment["user_id"],-delta,"stripe_refund_or_dispute",event["id"]+"-debit",now_iso()))
                    if event["type"]=="charge.refunded":
                        db.execute("UPDATE stripe_payments SET refunded_microusd=refunded_microusd+? WHERE payment_intent=?",(delta,payment_intent))
                    if event["type"]=="charge.dispute.created":
                        db.execute("INSERT INTO stripe_disputes(dispute_id,payment_intent,user_id,microusd,status) VALUES(?,?,?,?,?) ON CONFLICT(dispute_id) DO NOTHING",(obj.get("id",event["id"]),payment_intent,payment["user_id"],delta,"open"))
            elif payment_intent and target_amount:
                db.execute("INSERT INTO stripe_pending(id,payment_intent,microusd,kind,created_at) VALUES(?,?,?,?,?)",(event["id"],payment_intent,target_amount,event["type"],now_iso()))
                if event["type"]=="charge.dispute.created":
                    db.execute("INSERT INTO stripe_pending_disputes(dispute_id,payment_intent,microusd) VALUES(?,?,?) ON CONFLICT(dispute_id) DO NOTHING",(obj.get("id",event["id"]),payment_intent,target_amount))
        if event["type"]=="charge.dispute.closed" and obj.get("status")=="won":
            dispute=db.execute("SELECT * FROM stripe_disputes WHERE dispute_id=?",(obj.get("id"),)).fetchone()
            if dispute and dispute["status"]=="open":
                db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(dispute["user_id"],dispute["microusd"],"stripe_dispute_won",event["id"]+"-credit",now_iso()))
                db.execute("UPDATE stripe_disputes SET status='won' WHERE dispute_id=?",(obj.get("id"),))
            elif obj.get("id"):
                db.execute("INSERT INTO stripe_pending_dispute_closures(dispute_id,status) VALUES(?,?) ON CONFLICT(dispute_id) DO UPDATE SET status=excluded.status",(obj["id"],"won"))
        db.execute("INSERT INTO stripe_events(id,created_at) VALUES(?,?)",(event["id"],now_iso()))
    return {"received":True}

def validate_body(body:dict,multimodal:bool):
    if not isinstance(body,dict): raise HTTPException(400,"Request body must be an object")
    if "state" not in body: raise HTTPException(400,"state is required")
    questions=body.get("questions")
    if not isinstance(questions,dict) or not questions: raise HTTPException(400,"questions must be a non-empty object")
    for name,q in questions.items():
        if not isinstance(name,str) or not name or not isinstance(q,dict): raise HTTPException(400,"Each question must be a named object")
        typ=q.get("type")
        if typ not in ("choice","noul","score"): raise HTTPException(400,f"Unsupported question type for {name}")
        if "instructions" in q and not isinstance(q["instructions"],str): raise HTTPException(400,f"instructions must be text for {name}")
        criteria=q.get("criteria")
        if typ=="choice" and (not isinstance(criteria,dict) or len(criteria)<2): raise HTTPException(400,f"{name} choice requires a criteria object with at least two options")
        if typ=="score" and (not isinstance(criteria,list) or len(criteria)<2): raise HTTPException(400,f"{name} score requires a criteria list with at least two levels")
    fallback=body.get("fallback",[])
    if not isinstance(fallback,list) or any(not isinstance(x,str) for x in fallback): raise HTTPException(400,"fallback must be a list of model IDs")
    if body.get("model") is not None and not isinstance(body.get("model"),str): raise HTTPException(400,"model must be a model ID")
    choices=([body["model"]] if body.get("model") else [])+fallback
    if not choices: raise HTTPException(400,{"error":"model_required","options":list(MODELS)})
    if len(choices)!=len(set(choices)): raise HTTPException(400,"Duplicate models in route")
    unknown=[m for m in choices if m not in MODELS]
    if unknown: raise HTTPException(400,{"error":"unknown_model","models":unknown})
    if "classifier-fast" in choices and any(q.get("type")=="score" for q in questions.values()):
        raise HTTPException(400,"classifier-fast does not support score questions")
    images=body.get("images") or []
    if multimodal and not images: raise HTTPException(400,"images are required")
    if not multimodal and images: raise HTTPException(400,"Use /v1/multimodal for image requests")
    if images:
        if len(images)>MAX_IMAGES: raise HTTPException(413,f"At most {MAX_IMAGES} images")
        for value in images:
            match=re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)",value or "")
            if not match: raise HTTPException(400,"Images must be PNG, JPEG, or WebP data URLs")
            try: raw=base64.b64decode(match.group(2),validate=True)
            except Exception: raise HTTPException(400,"Malformed base64 image")
            if len(raw)>MAX_IMAGE_BYTES: raise HTTPException(413,f"Each image must be at most {MAX_IMAGE_BYTES} bytes")
        bad=[m for m in choices if "image" not in MODELS[m]["modalities"]]
        if bad: raise HTTPException(400,{"error":"model_not_multimodal","models":bad})
    return choices

async def post_json(url:str,body:dict,headers:dict,timeout=120):
    async with httpx.AsyncClient(timeout=timeout) as client:
        r=await client.post(url,json=body,headers=headers); r.raise_for_status(); return r.json()

async def call_model(model:str,body:dict,request:Request):
    payload={k:v for k,v in body.items() if k not in ("model","fallback")}
    if model=="stripe-test-paid":
        if STRIPE_MODE != "test" or not PAYMENTS_ENABLED:
            raise ValueError("test billing route is disabled")
        answers={}
        for name,q in (body.get("questions") or {}).items():
            if q.get("type")=="noul": answers[name]={"type":"noul","noul":0.75}
            elif q.get("type")=="choice":
                option=next(iter(q["criteria"]))
                answers[name]={"type":"choice","choice":option,"confidence":1.0}
            else: answers[name]={"type":"score","score":0}
        return {"model":model,"answers":answers,"usage":{"estimated_cost_usd":0.0001,"test_mode":True}}
    if model=="classifier-fast":
        answers={}
        async with httpx.AsyncClient(timeout=15) as client:
            for name,q in (body.get("questions") or {}).items():
                labels=["yes","no"] if q.get("type")=="noul" else list((q.get("criteria") or {}).keys())
                if len(labels)<2: raise ValueError("question requires at least two criteria")
                prompt=str(body.get("state"))+("\n\n"+q["instructions"] if q.get("instructions") else "")
                r=await client.post("https://classifier.dev/v1/classify",json={"inputs":[prompt],"labels":labels,"tier":"fast"}); r.raise_for_status(); item=r.json()["results"][0]; scores=item.get("scores",{})
                answers[name]={"type":q["type"],"noul":float(scores.get("yes",0))} if q["type"]=="noul" else {"type":q["type"],"choice":item["label"],"confidence":item.get("confidence"),"probabilities":scores}
        return {"model":model,"answers":answers,"usage":{"estimated_cost_usd":0}}
    if model in ("jev-typesafe","jev-vercel"):
        raise ValueError("disabled pending written standalone-gateway permission; use the provider directly")
    endpoint=os.getenv({"semif-qwen3.5-4b":"SEMIF_ENDPOINT","djev":"DJEV_ENDPOINT","decider-2b-vision":"DECIDER_ENDPOINT","laya-421m":"LAYA_ENDPOINT"}[model],"").rstrip("/")
    if not endpoint: raise ValueError("scale-to-zero endpoint is offline")
    token=os.getenv({"semif-qwen3.5-4b":"SEMIF_API_KEY","djev":"DJEV_API_KEY","decider-2b-vision":"DECIDER_API_KEY","laya-421m":"LAYA_API_KEY"}[model],"")
    return await post_json(endpoint+"/v1/request",payload,{"Authorization":"Bearer "+token} if token else {})

def reserve_credit(user_id:int, microusd:int, ref:str) -> None:
    if not microusd:return
    reconcile_stale_reservations()
    today=datetime.now(timezone.utc).date().isoformat()
    with dbconn() as db:
        if DATABASE_URL:
            db.execute("SELECT pg_advisory_xact_lock(?)",(9_876_543,)); db.execute("SELECT pg_advisory_xact_lock(?)",(user_id,))
        else: db.execute("BEGIN IMMEDIATE")
        available=int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM credit_events WHERE user_id=?",(user_id,)).fetchone()["total"])
        if available<microusd: raise HTTPException(402,"Insufficient prepaid credits")
        global_spend=int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM usage_events WHERE created_at>=?",(today,)).fetchone()["total"])+int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM spend_reservations WHERE created_at>=?",(today,)).fetchone()["total"])
        if global_spend+microusd>OPERATOR_DAILY_CAP_CENTS*10_000: raise HTTPException(503,"Operator daily spend cap reached")
        user_spend=int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM usage_events WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["total"])+int(db.execute("SELECT COALESCE(SUM(microusd),0) total FROM spend_reservations WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["total"])
        row=db.execute("SELECT daily_spend_cap_microusd FROM account_settings WHERE user_id=?",(user_id,)).fetchone(); cap=int(row["daily_spend_cap_microusd"]) if row else 100_000_000
        if user_spend+microusd>cap: raise HTTPException(402,"Account daily spend cap reached")
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(user_id,-microusd,"usage_reservation",ref,now_iso()))
        db.execute("INSERT INTO spend_reservations(ref,user_id,microusd,created_at) VALUES(?,?,?,?)",(ref,user_id,microusd,now_iso()))

def refund_reservation(user_id:int, microusd:int, ref:str) -> None:
    if not microusd:return
    with dbconn() as db:
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(user_id,microusd,"failed_request_refund","refund-"+ref,now_iso()))
        db.execute("DELETE FROM spend_reservations WHERE ref=?",(ref,))

def reconcile_stale_reservations(max_age_seconds:int=600) -> int:
    cutoff=datetime.fromtimestamp(time.time()-max_age_seconds,timezone.utc).isoformat(); recovered=0
    with dbconn() as db:
        if DATABASE_URL: db.execute("SELECT pg_advisory_xact_lock(?)",(9_876_543,))
        else: db.execute("BEGIN IMMEDIATE")
        rows=db.execute("SELECT * FROM spend_reservations WHERE created_at<?",(cutoff,)).fetchall()
        for row in rows:
            db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?) ON CONFLICT(ref) DO NOTHING",(row["user_id"],row["microusd"],"stale_request_refund","refund-"+row["ref"],now_iso()))
            db.execute("DELETE FROM spend_reservations WHERE ref=?",(row["ref"],)); recovered+=1
    return recovered

async def decide(request:Request,multimodal=False):
    if int(request.headers.get("content-length","0") or 0)>MAX_BODY: raise HTTPException(413,"Request too large")
    user=api_user(request.headers.get("authorization")); enforce_rate_limit(user["key_id"]); body=await request.json(); choices=validate_body(body,multimodal); errors=[]
    for model in choices:
        info=MODELS[model]; microusd=int(info["price_per_1k_cents"]*10)  # cents/1000 -> micro-USD/decision
        reservation=secrets.token_hex(16); reserve_credit(user["id"],microusd,reservation)
        started=time.perf_counter()
        try:
            out=await call_model(model,body,request); ms=round((time.perf_counter()-started)*1000,1)
            with dbconn() as db:
                db.execute("INSERT INTO usage_events(user_id,api_key_id,model,microusd,provider,latency_ms,created_at) VALUES(?,?,?,?,?,?,?)",(user["id"],user["key_id"],model,microusd,info["provider"],ms,now_iso()))
                db.execute("DELETE FROM spend_reservations WHERE ref=?",(reservation,))
            return JSONResponse(out,headers={"X-Jev-Provider":info["provider"],"X-Jev-Model":model,"X-Jev-Latency-Ms":str(ms),"X-Jev-Cost-Usd":f"{microusd/1_000_000:.6f}","X-Jev-No-Markup":"true" if info["billing"].startswith("third-party") else "not-applicable","Cache-Control":"no-store"})
        except HTTPException: raise
        except asyncio.CancelledError:
            refund_reservation(user["id"],microusd,reservation); raise
        except Exception as e:
            refund_reservation(user["id"],microusd,reservation)
            errors.append({"model":model,"error":str(e)[:160]})
    raise HTTPException(502,{"error":"all_providers_failed","attempts":errors})

@app.post("/v1/systemone")
async def systemone(request:Request): return await decide(request,False)

@app.post("/v1/multimodal")
async def multimodal(request:Request): return await decide(request,True)

@app.post("/v1/chat/completions")
async def chat(request:Request):
    body=await request.json(); messages=body.get("messages") or []
    if not messages: raise HTTPException(400,"messages required")
    try: native=json.loads(messages[-1]["content"])
    except Exception: raise HTTPException(400,"Final message content must be a JSON System One request")
    native["model"]=body.get("model"); native["fallback"]=body.get("fallback",[])
    class Wrapped:
        headers=request.headers
        async def json(self): return native
    response=await decide(Wrapped(),bool(native.get("images"))); data=json.loads(response.body)
    return JSONResponse({"id":"jev-"+secrets.token_hex(8),"object":"chat.completion","created":int(time.time()),"model":body.get("model"),"choices":[{"index":0,"message":{"role":"assistant","content":json.dumps(data)},"finish_reason":"stop"}]},headers=dict(response.headers))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","8080")))
