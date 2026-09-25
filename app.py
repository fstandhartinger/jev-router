"""Jev Router product gateway.

Concrete models remain explicitly selectable. The two documented meta model
names use a public, deterministic score order among providers healthy now.
Request bodies (including images) are never logged or stored.
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

import routing

APP_URL = os.getenv("APP_URL", "http://localhost:8080").rstrip("/")
DB_PATH = os.getenv("DATABASE_PATH", "/data/jev-router.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-development-only-change-me")
# Payments mode is explicit. Live needs STRIPE_MODE=live, live keys and PAYMENTS_ENABLED_LIVE=true;
# key prefixes alone never switch modes, and test keys are never used in live mode (or vice versa).
STRIPE_MODE = "live" if os.getenv("STRIPE_MODE", "test").lower() == "live" else "test"
if STRIPE_MODE == "live":
    STRIPE_SECRET_KEY = os.getenv("STRIPE_LIVE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_LIVE_WEBHOOK_SECRET", "")
    PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED_LIVE", "false").lower() == "true" and STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_")) and bool(STRIPE_WEBHOOK_SECRET)
else:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_TEST_SECRET_KEY") or os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_TEST_WEBHOOK_SECRET") or os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if STRIPE_SECRET_KEY and not STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")): STRIPE_SECRET_KEY = ""
    PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED_TEST", "false").lower() == "true"
MAX_TOPUP_CENTS = int(os.getenv("MAX_TOPUP_CENTS", "50000"))
DAILY_TOPUP_CAP_CENTS = int(os.getenv("DAILY_TOPUP_CAP_CENTS", "100000"))
CHECKOUTS_PER_HOUR = int(os.getenv("CHECKOUTS_PER_HOUR", "5"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "jev-router.com")
ALIAS_HOSTS = {h.strip().lower() for h in os.getenv("ALIAS_HOSTS", "www.jev-router.com,jev-router.app.mintapis.com,decision-models.com,www.decision-models.com,decisionmodels.io,www.decisionmodels.io,decisionmodels.cloud,www.decisionmodels.cloud,decisionmodels.online,www.decisionmodels.online,system-one.io,www.system-one.io,system-one.cloud,www.system-one.cloud,system-one.online,www.system-one.online").split(",") if h.strip()}
STRIPE_AUTOMATIC_TAX = os.getenv("STRIPE_AUTOMATIC_TAX", "false").lower() == "true"
MIN_TOPUP_CENTS = int(os.getenv("MIN_TOPUP_CENTS", "1000"))
OPERATOR_DAILY_CAP_CENTS = int(os.getenv("OPERATOR_DAILY_CAP_CENTS", "50000"))
HOSTING_CONTROL_URL = os.getenv("HOSTING_CONTROL_URL", "").rstrip("/")
HOSTING_CONTROL_TOKEN = os.getenv("HOSTING_CONTROL_TOKEN", "")
HOSTING_ACCOUNT_MAX = int(os.getenv("HOSTING_ACCOUNT_MAX", "1"))
HOSTING_GLOBAL_MAX = int(os.getenv("HOSTING_GLOBAL_MAX", "4"))
HOSTING_DAILY_PROVIDER_CAP_MICROUSD = int(os.getenv("HOSTING_DAILY_PROVIDER_CAP_MICROUSD", "40000000"))
HOSTING_MARGIN_PERCENT = int(os.getenv("HOSTING_MARGIN_PERCENT", "10"))
HOSTING_REAPER_SECONDS = int(os.getenv("HOSTING_REAPER_SECONDS", "120"))
MAX_BODY = 2_500_000
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2_000_000
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "4"))

CATALOGUE = routing.load_catalogue()
HEALTH = routing.Health()

def billing_text(entry: dict[str, Any]) -> str:
    if entry["routing"]=="paid": return f'USD {entry["price_per_1k_cents"]/100:.2f} / 1,000 decisions'
    if entry["routing"]=="free": return "Free (third-party service; we add no charge)"
    if entry["routing"]=="byok": return "Your own provider key; we add no charge"
    return "Not routed (listed for comparison)"

def entry_endpoint(entry: dict[str, Any]) -> str:
    return (os.getenv(entry["base_url_env"],"") if entry.get("base_url_env") else entry.get("base_url") or "").rstrip("/")

def build_models() -> dict[str, dict[str, Any]]:
    models={}
    for model, entry in CATALOGUE.items():
        if entry["routing"]=="listed": continue
        configured=entry["adapter"]=="classifier.dev" or bool(entry_endpoint(entry))
        models[model]={"provider":entry["vendor"],"display":entry["display"],"status":"live" if configured else "offline","configured":configured,
            "routing":entry["routing"],"modalities":entry["modalities"],"question_types":entry["question_types"],
            "price_per_1k_cents":entry.get("price_per_1k_cents",0) if entry["routing"]=="paid" else 0,"billing":billing_text(entry),
            "jevbench":entry.get("jevbench"),"imagejevbench":entry.get("imagejevbench"),"licence":entry.get("licence"),
            "terms":entry["terms_basis"],"terms_url":entry.get("terms_url"),"homepage":entry.get("homepage")}
    return models

MODELS: dict[str, dict[str, Any]] = build_models()
ON_DEMAND_MODELS: dict[str, dict[str, Any]] = {
    "djev-spark": {
        "provider": "lium.io (RunPod fallback)", "gpu": "RTX 5090 32 GB",
        "modalities": ["text", "image"], "provider_microusd_per_minute": 11_334,
        "cold_start_seconds": 240,
        "terms": "Open Apache-2.0 runtime and weights; billed only while your GPU instance is hosted.",
    },
    "djev-bf16": {
        "provider": "lium.io (RunPod fallback)", "gpu": "RTX PRO 6000 96 GB",
        "modalities": ["text", "image"], "provider_microusd_per_minute": 21_500,
        "cold_start_seconds": 300,
        "terms": "Open Apache-2.0 runtime and weights; billed only while your GPU instance is hosted.",
    },
}
for _hosting in ON_DEMAND_MODELS.values():
    _hosting["margin_percent"] = HOSTING_MARGIN_PERCENT
    _hosting["price_microusd_per_minute"] = (_hosting["provider_microusd_per_minute"] * (100 + HOSTING_MARGIN_PERCENT) + 99) // 100
META_MODELS: dict[str, dict[str, Any]] = {
    "auto": {"provider":"Jev Router published routing rule","modalities":["text","image"],"price_per_1k_cents":None,"billing":"The price of the concrete model that answers.","terms":"Picks the best healthy model for the request's modality, question types and route preferences; the next one is the automatic fallback."},
    "jev-class": {"provider":"Jev Router published routing rule","modalities":["text"],"price_per_1k_cents":None,"billing":"The price of the concrete model that answers.","terms":"Alias of auto with prefer=quality for text: highest JevBench score among healthy models."},
    "image-jev-class": {"provider":"Jev Router published routing rule","modalities":["text","image"],"price_per_1k_cents":None,"billing":"The price of the concrete model that answers.","terms":"Alias of auto with prefer=quality for images: highest ImageJevBench score among healthy image models."},
}
if STRIPE_MODE == "test" and PAYMENTS_ENABLED:
    MODELS["stripe-test-paid"] = {"provider":"Jev Router test fixture","display":"Stripe test fixture","status":"live","configured":True,"routing":"paid","modalities":["text"],"question_types":["choice","noul","score"],
        "price_per_1k_cents":10,"billing":"USD 0.10 / 1,000 decisions; Stripe test mode only","jevbench":None,"imagejevbench":None,"terms":"Deterministic non-production route for billing tests."}
    CATALOGUE["stripe-test-paid"] = {"id":"stripe-test-paid","routing":"paid","modalities":["text"],"question_types":["choice","noul","score"],"price_per_1k_cents":10,"adapter":"fixture","jevbench":None}

HEALTH_TASK: asyncio.Task | None = None
HOSTING_TASK: asyncio.Task | None = None

def concrete_status(model: str) -> str:
    if not MODELS[model].get("configured",True): return "offline"
    return HEALTH.status(model) if model!="stripe-test-paid" else "live"

def route_candidates(model: str, *, image: bool, question_types: set[str], provider_keys: dict[str, str] | None = None, route: dict[str, Any] | None = None) -> list[str]:
    route=route or {}
    prefer=route.get("prefer","quality") if model=="auto" else "quality"
    routable={k:v for k,v in CATALOGUE.items() if k in MODELS and MODELS[k].get("configured",True) and k!="stripe-test-paid"}
    return routing.rank(routable, HEALTH, image=image, question_types=question_types, provider_keys=provider_keys or {},
        prefer=prefer, task=route.get("task") if model=="auto" else None, max_price_per_1k=route.get("max_price_per_1k"),
        max_latency_ms=route.get("max_latency_ms"), benchmarked_only=model!="auto" or bool(route.get("benchmarked_only",False)))

def meta_candidates(meta: str) -> list[str]:
    image=meta=="image-jev-class"
    return route_candidates(meta, image=image, question_types={"choice"})

def public_models() -> dict[str, dict[str, Any]]:
    result={}
    for k,v in MODELS.items():
        h=HEALTH.get(k)
        result[k]={**v,"status":concrete_status(k),"observed_latency_ms":h["ewma_ms"]}
    for meta, info in META_MODELS.items():
        candidates=meta_candidates("jev-class" if meta=="auto" else meta)
        result[meta]={**info,"status":"live" if candidates else "offline","routing_order":candidates}
    return result

def listed_models() -> list[dict[str, Any]]:
    return [v for v in CATALOGUE.values() if v["routing"]=="listed"]

async def probe_one(client: httpx.AsyncClient, model: str, entry: dict[str, Any]) -> None:
    url=entry.get("health_url") or (entry_endpoint(entry)+entry["health_path"] if entry.get("health_path") and entry_endpoint(entry) else "")
    if not url: return
    started=time.perf_counter()
    try:
        response=await client.get(url)
        if response.status_code<500: HEALTH.ok(model, None if entry.get("health_is_cheap",True) else round((time.perf_counter()-started)*1000,1))
        else: HEALTH.fail(model,f"health HTTP {response.status_code}")
    except Exception as exc:
        HEALTH.fail(model,f"health {type(exc).__name__}")

async def probe_providers() -> None:
    while True:
        async with httpx.AsyncClient(timeout=8) as client:
            await asyncio.gather(*(probe_one(client,m,CATALOGUE[m]) for m in MODELS if m in CATALOGUE and MODELS[m].get("configured")),return_exceptions=True)
        await asyncio.sleep(30)

app = FastAPI(title="Jev Router", docs_url=None, redoc_url=None, openapi_url="/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=APP_URL.startswith("https://"), max_age=86400 * 14)

@app.middleware("http")
async def canonical_host(request: Request, call_next):
    host=(request.url.hostname or "").lower()
    if host in ALIAS_HOSTS and host!=CANONICAL_HOST:
        target = "https://"+CANONICAL_HOST + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        # 301 for page views; 308 keeps POST bodies intact for API clients on old hosts.
        return RedirectResponse(target, status_code=301 if request.method in ("GET","HEAD") else 308)
    return await call_next(request)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"strict-origin-when-cross-origin","Permissions-Policy":"camera=(), microphone=(), geolocation=()","Content-Security-Policy":"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; base-uri 'self'"})
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
        CREATE TABLE IF NOT EXISTS hosting_settings(user_id BIGINT PRIMARY KEY, idle_minutes BIGINT NOT NULL DEFAULT 10);
        CREATE TABLE IF NOT EXISTS hosting_instances(id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL, provider_instance_id TEXT UNIQUE, status TEXT NOT NULL, endpoint TEXT, price_microusd_per_minute BIGINT NOT NULL, provider_microusd_per_minute BIGINT NOT NULL, billed_minutes BIGINT NOT NULL DEFAULT 0, started_at TEXT NOT NULL, last_billed_at TEXT NOT NULL, last_used_at TEXT NOT NULL, stopped_at TEXT, stop_reason TEXT);
        CREATE TABLE IF NOT EXISTS hosting_minute_events(instance_id TEXT NOT NULL, minute_number BIGINT NOT NULL, user_id BIGINT NOT NULL, price_microusd BIGINT NOT NULL, provider_microusd BIGINT NOT NULL, usage_event_id BIGINT, created_at TEXT NOT NULL, PRIMARY KEY(instance_id,minute_number));
        """
        with psycopg.connect(DATABASE_URL) as db:
            for statement in ddl.split(";"):
                if statement.strip(): db.execute(statement)
            db.execute("ALTER TABLE hosting_minute_events ADD COLUMN IF NOT EXISTS usage_event_id BIGINT")
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
        CREATE TABLE IF NOT EXISTS hosting_settings(user_id INTEGER PRIMARY KEY, idle_minutes INTEGER NOT NULL DEFAULT 10);
        CREATE TABLE IF NOT EXISTS hosting_instances(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL, provider_instance_id TEXT UNIQUE, status TEXT NOT NULL, endpoint TEXT, price_microusd_per_minute INTEGER NOT NULL, provider_microusd_per_minute INTEGER NOT NULL, billed_minutes INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, last_billed_at TEXT NOT NULL, last_used_at TEXT NOT NULL, stopped_at TEXT, stop_reason TEXT);
        CREATE TABLE IF NOT EXISTS hosting_minute_events(instance_id TEXT NOT NULL, minute_number INTEGER NOT NULL, user_id INTEGER NOT NULL, price_microusd INTEGER NOT NULL, provider_microusd INTEGER NOT NULL, usage_event_id INTEGER, created_at TEXT NOT NULL, PRIMARY KEY(instance_id,minute_number));
        """)
            columns={row[1] for row in db.execute("PRAGMA table_info(hosting_minute_events)").fetchall()}
            if "usage_event_id" not in columns: db.execute("ALTER TABLE hosting_minute_events ADD COLUMN usage_event_id INTEGER")

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
        if row and int(row["requests"])>=RATE_LIMIT_PER_MINUTE: raise HTTPException(429,"API key rate limit exceeded",headers={"Retry-After":"60"})
        db.execute("INSERT INTO rate_windows(api_key_id,bucket_window,requests) VALUES(?,?,1) ON CONFLICT(api_key_id,bucket_window) DO UPDATE SET requests=rate_windows.requests+1",(key_id,window))

def esc(value: Any) -> str:
    import html
    return html.escape(str(value))

CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--panel:#fff;--text:#171717;--muted:#666;--line:#ddd;--accent:#5d5fef;--soft:#f3f3f7}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}nav,main,footer{max-width:1120px;margin:auto;padding:20px 28px}nav{display:flex;align-items:center;gap:24px;border-bottom:1px solid var(--line)}nav .brand{font-weight:750;font-size:18px;margin-right:auto;color:var(--text)}a{color:inherit;text-decoration:none}nav a:not(.brand),.muted{color:var(--muted)}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:3px}.skip{position:absolute;left:-9999px}.skip:focus{left:12px;top:12px;background:var(--panel);padding:10px;z-index:10}.hero{padding:92px 0 68px;max-width:780px}.eyebrow{font-size:13px;color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.08em}h1{font-size:clamp(42px,7vw,72px);line-height:1.02;letter-spacing:-.055em;margin:16px 0 24px}h2{font-size:30px;letter-spacing:-.03em;margin-top:48px}h3{margin:0 0 8px}.lead{font-size:20px;color:var(--muted);max-width:700px}.actions{display:flex;gap:12px;margin-top:30px;flex-wrap:wrap}.button,button{display:inline-flex;border:1px solid var(--line);background:var(--panel);padding:10px 16px;border-radius:8px;font:inherit;cursor:pointer}.primary{background:var(--text);color:var(--bg);border-color:var(--text)}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px}.badge{display:inline-block;padding:3px 8px;border-radius:99px;background:var(--soft);font-size:12px}.live{color:#087a45}.offline{color:#946200}.byok{color:#5d5fef}code,pre{font:13px/1.5 ui-monospace,SFMono-Regular,monospace}pre{padding:18px;background:#111218;color:#e8e8ec;border-radius:10px;overflow:auto}.notice{border-left:3px solid var(--accent);padding:10px 16px;background:var(--soft)}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:11px;border-bottom:1px solid var(--line);vertical-align:top}input{width:100%;max-width:420px;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text)}footer{color:var(--muted);margin-top:70px;border-top:1px solid var(--line);display:flex;gap:18px;flex-wrap:wrap}@media(max-width:760px){nav{gap:12px;flex-wrap:wrap;padding:16px}nav .brand{flex-basis:100%;margin-right:0}nav a:not(.brand){white-space:nowrap}.hero{padding:55px 0}.grid{grid-template-columns:1fr}main{padding:16px}h1{font-size:44px}}@media(prefers-color-scheme:dark){:root{--bg:#0e0f12;--panel:#15161a;--text:#f2f2f3;--muted:#a0a0a7;--line:#292a30;--accent:#8b8dff;--soft:#1d1e24}}
"""

DISCLAIMER="Jev is a trademark of TypeSafe AI, Inc. Jev Router is an independent service by productivity-boost.com Betriebs UG &amp; Co. KG and is not affiliated with, endorsed by, or sponsored by TypeSafe AI. We do not provide access to TypeSafe's Jev model. &ldquo;Jev-class&rdquo; describes the typed decision-API format (choice, noul, score) only."

def page(title: str, body: str, user=None) -> HTMLResponse:
    auth = '<a href="/dashboard">Dashboard</a><a href="/logout">Sign out</a>' if user else '<a href="/login">Sign in</a>'
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="One API for open decision models (Jev-class): choice, yes/no and score decisions routed by published benchmark scores, price and latency."><title>{esc(title)} · Jev Router</title><style>{CSS}</style></head><body><a class="skip" href="#main">Skip to content</a><nav aria-label="Main navigation"><a class="brand" href="/">Jev Router</a><a href="/models-page">Models</a><a href="/pricing">Pricing</a><a href="/docs">Docs</a><a href="/status">Status</a>{auth}</nav><main id="main">{body}</main><footer><span>© 2026 productivity-boost.com Betriebs UG &amp; Co. KG</span><a href="/pricing">Pricing</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a><a href="/refunds">Refunds</a><a href="/impressum">Impressum</a><p class="muted" style="flex-basis:100%;font-size:13px;margin:0">{DISCLAIMER}</p></footer></body></html>''')

@app.on_event("startup")
async def startup():
    global HEALTH_TASK, HOSTING_TASK
    ensure_db(); reconcile_stale_reservations()
    HEALTH_TASK=asyncio.create_task(probe_providers()) if os.getenv("PROBE_PROVIDERS","true").lower()!="false" else None
    HOSTING_TASK=asyncio.create_task(hosting_reaper_loop()) if HOSTING_CONTROL_URL and HOSTING_CONTROL_TOKEN else None

@app.on_event("shutdown")
async def shutdown():
    if HEALTH_TASK:
        HEALTH_TASK.cancel()
        try: await HEALTH_TASK
        except asyncio.CancelledError: pass
    if HOSTING_TASK:
        HOSTING_TASK.cancel()
        try: await HOSTING_TASK
        except asyncio.CancelledError: pass

@app.get("/health")
def health(): return {"ok": True, "stripe_mode": STRIPE_MODE, "payments_enabled": PAYMENTS_ENABLED, "hosting_control_configured":bool(HOSTING_CONTROL_URL and HOSTING_CONTROL_TOKEN)}

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/hosting/models")
def hosting_models():
    return {"object":"list","data":[{"id":k,**v,"available":bool(HOSTING_CONTROL_URL and HOSTING_CONTROL_TOKEN)} for k,v in ON_DEMAND_MODELS.items()],"billing":"Per started minute of hosted time, prepaid; price includes the disclosed margin."}

@app.get("/hosting/instances")
def hosting_instances(request: Request):
    u=require_user(request)
    with dbconn() as db: rows=db.execute("SELECT * FROM hosting_instances WHERE user_id=? ORDER BY started_at DESC LIMIT 50",(u["id"],)).fetchall()
    now=datetime.now(timezone.utc)
    return {"data":[{**dict(x),"meter_microusd":int(x["billed_minutes"])*int(x["price_microusd_per_minute"]),"hosted_seconds":max(0,int((datetime.fromisoformat(x["stopped_at"]) if x["stopped_at"] else now-datetime.fromisoformat(x["started_at"])).total_seconds())) if not x["stopped_at"] else max(0,int((datetime.fromisoformat(x["stopped_at"])-datetime.fromisoformat(x["started_at"])).total_seconds()))} for x in rows]}

@app.post("/hosting/settings")
async def hosting_settings(request: Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf")); idle=int(form.get("idle_minutes",10))
    if idle<2 or idle>60: raise HTTPException(400,"Idle shutdown must be between 2 and 60 minutes")
    with dbconn() as db: db.execute("INSERT INTO hosting_settings(user_id,idle_minutes) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET idle_minutes=excluded.idle_minutes",(u["id"],idle))
    return RedirectResponse("/dashboard",303)

@app.post("/hosting/start")
async def hosting_start(request: Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf")); model=str(form.get("model",""))
    if model not in ON_DEMAND_MODELS: raise HTTPException(400,"Unknown on-demand model")
    quote=await hosting_control("POST","/quote",{"model":model})
    provider_cost=int(quote["provider_microusd_per_minute"])
    spec=ON_DEMAND_MODELS[model]
    if quote.get("model")!=model or quote.get("provider") not in ("lium","runpod"): raise HTTPException(503,"Provider returned an invalid quote")
    if provider_cost<=0 or provider_cost>int(spec["provider_microusd_per_minute"]): raise HTTPException(409,"Current provider price exceeds the displayed price; reload after the catalogue updates")
    try: quote_expiry=datetime.fromisoformat(str(quote["expires_at"]).replace("Z","+00:00"))
    except Exception: raise HTTPException(503,"Provider quote has no valid expiry")
    if quote_expiry<=datetime.now(timezone.utc): raise HTTPException(409,"Provider quote expired; retry")
    price=(provider_cost*(100+HOSTING_MARGIN_PERCENT)+99)//100
    iid=secrets.token_hex(16); now=now_iso()
    with dbconn() as db:
        if DATABASE_URL: db.execute("SELECT pg_advisory_xact_lock(?)",(9_876_544,)); db.execute("SELECT pg_advisory_xact_lock(?)",(u["id"],))
        else: db.execute("BEGIN IMMEDIATE")
        account=int(db.execute("SELECT COUNT(*) n FROM hosting_instances WHERE user_id=? AND status IN ('starting','running','stopping')",(u["id"],)).fetchone()["n"])
        total=int(db.execute("SELECT COUNT(*) n FROM hosting_instances WHERE status IN ('starting','running','stopping')").fetchone()["n"])
        if account>=HOSTING_ACCOUNT_MAX: raise HTTPException(429,"Account concurrent-instance limit reached")
        if total>=HOSTING_GLOBAL_MAX: raise HTTPException(503,"Global concurrent-instance limit reached")
        db.execute("INSERT INTO hosting_instances(id,user_id,model,provider,status,price_microusd_per_minute,provider_microusd_per_minute,billed_minutes,started_at,last_billed_at,last_used_at) VALUES(?,?,?,?,'starting',?,?,0,?,?,?)",(iid,u["id"],model,str(quote["provider"]),price,provider_cost,now,now,now))
    if not charge_hosting_minute(iid,1):
        with dbconn() as db: db.execute("UPDATE hosting_instances SET status='stopped',stopped_at=?,stop_reason='insufficient prepaid credit or spend cap' WHERE id=?",(now_iso(),iid))
        raise HTTPException(402,"Insufficient prepaid credit or spend cap")
    try:
        remote=await hosting_control("POST","/instances",{"model":model,"managed_by":"jev-router","owner":iid,"quote_id":quote["quote_id"]})
        if int(remote["provider_microusd_per_minute"]) != provider_cost or str(remote["provider"]) != str(quote["provider"]): raise RuntimeError("provider changed the accepted quote")
        with dbconn() as db:
            db.execute("UPDATE hosting_instances SET provider_instance_id=?,endpoint=?,status='running' WHERE id=?",(str(remote["id"]),remote.get("endpoint"),iid))
    except Exception:
        refund_hosting_minute(iid,1,"provisioning failed")
        with dbconn() as db: db.execute("UPDATE hosting_instances SET status='stopped',stopped_at=?,stop_reason='provisioning failed' WHERE id=?",(now_iso(),iid))
        raise
    return RedirectResponse("/dashboard",303)

@app.post("/hosting/{instance_id}/stop")
async def hosting_stop(instance_id: str, request: Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf"))
    with dbconn() as db: row=db.execute("SELECT * FROM hosting_instances WHERE id=? AND user_id=?",(instance_id,u["id"])).fetchone()
    if not row: raise HTTPException(404,"Instance not found")
    await stop_hosting_instance(row,"stopped by user")
    return RedirectResponse("/dashboard",303)

@app.post("/hosting/{instance_id}/v1/systemone")
async def hosted_inference(instance_id: str, request: Request):
    user=api_user(request.headers.get("authorization")); enforce_rate_limit(user["key_id"])
    if int(request.headers.get("content-length","0") or 0)>MAX_BODY: raise HTTPException(413,"Request too large")
    with dbconn() as db:
        row=db.execute("SELECT * FROM hosting_instances WHERE id=? AND user_id=?",(instance_id,user["id"])).fetchone()
        if not row: raise HTTPException(404,"Instance not found")
        if row["status"]!="running" or not row["endpoint"]: raise HTTPException(425,"Instance is not ready",headers={"Retry-After":"10"})
    payload=await request.json()
    result=await post_json(row["endpoint"].rstrip("/")+"/v1/systemone",payload,{},timeout=180)
    with dbconn() as db: db.execute("UPDATE hosting_instances SET last_used_at=? WHERE id=? AND status='running'",(now_iso(),instance_id))
    return result

ROUTING_POLICY=("Concrete model IDs are never rerouted except to the fallback list you send. "
    "auto (default) ranks healthy models that support the request's modality and question types. "
    "prefer=quality (default): highest published score first (JevBench v1.4.2 for text, ImageJevBench v0.1 for images; with route.task, the matching JevBench tier). "
    "prefer=price: lowest price first. prefer=latency: lowest observed median latency first. "
    "prefer=balanced: score minus 10 points per second of latency and minus 10 points per tenfold price above USD 0.01 per 1,000 decisions. "
    "Ties break by price, then latency, then model ID. Up to 4 models are tried in order; failed attempts are never charged. "
    "Unbenchmarked models are used by auto only after all benchmarked ones. "
    "jev-class and image-jev-class are aliases of auto with prefer=quality restricted to benchmarked models.")

HOME_EXAMPLE = """curl https://jev-router.com/v1/systemone -H "Authorization: Bearer jvr_..." -H "Content-Type: application/json" -d '{
  "model": "auto",
  "state": "Customer: my parcel arrived crushed and the mug inside is broken.",
  "questions": {
    "intent": {"type": "choice", "criteria": {"refund": "wants money back", "replacement": "wants a new item", "info": "just asking"}},
    "upset":  {"type": "noul", "instructions": "Is the customer upset?"}
  }
}'"""

DOC_EXAMPLE = """curl https://jev-router.com/v1/systemone \\
  -H "Authorization: Bearer jvr_..." \\
  -H "Content-Type: application/json" \\
  -d '{"model":"auto","route":{"prefer":"quality","task":"judgement","max_price_per_1k":0.05},"state":"Mia owns a red bicycle.","questions":{"color":{"type":"choice","instructions":"What colour is the bicycle?","criteria":{"red":null,"blue":null}}}}'"""

IMAGE_EXAMPLE = """curl https://jev-router.com/v1/multimodal \\
  -H "Authorization: Bearer jvr_..." -H "Content-Type: application/json" \\
  -d '{"model":"auto","state":"Photo from a delivery driver.","images":["data:image/jpeg;base64,..."],"questions":{"damaged":{"type":"noul","instructions":"Is the parcel visibly damaged?"}}}'"""

OPENAI_EXAMPLE = """from openai import OpenAI
client = OpenAI(base_url="https://jev-router.com/v1", api_key="jvr_...")
r = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Cancel my subscription, this is the third outage this week."}],
    extra_body={"questions": {
        "churn_risk": {"type": "score", "criteria": ["none", "low", "medium", "high"]},
        "topic": {"type": "choice", "criteria": {"billing": None, "outage": None, "feature": None}}}},
)
print(r.choices[0].message.content)   # {"answers": {...}}
print(r.model)                        # the concrete model that answered"""

RESPONSE_EXAMPLE = """{"answers": {"color": {"type": "choice", "choice": "red", "confidence": 0.97}},
 "model": "laya-421m",
 "routing": {"requested": "auto", "answered_by": "laya-421m", "attempts": [], "price_usd_per_1k": 0.01, "billing": "paid"}}"""

def bench_cell(v: dict[str, Any]) -> str:
    parts=[]
    j=v.get("jevbench"); i=v.get("imagejevbench")
    if j and j.get("score") is not None: parts.append(f'JevBench {esc(j["version"])}: <strong>{float(j["score"]):.1f}</strong>'+(f' (#{j["rank"]})' if j.get("rank") else ''))
    if i and i.get("score") is not None: parts.append(f'ImageJevBench {esc(i["version"])}: <strong>{float(i["score"]):.1f}</strong>'+(f' (#{i["rank"]})' if i.get("rank") else ''))
    return "<br>".join(parts) or '<span class="muted">not benchmarked</span>'

def live_routes() -> list[str]:
    return [k for k,v in public_models().items() if k not in META_MODELS and k!="stripe-test-paid" and v["status"] in ("live","unknown") and v.get("configured",True)]

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user=current_user(request)
    live=live_routes()
    body=f'''<section class="hero"><div class="eyebrow">Open decision models · one API</div><h1>Typed decisions from the best open model that is up right now.</h1><p class="lead">Ask choice, yes/no and score questions about text or images. Jev Router picks the model from published JevBench and ImageJevBench scores, price and live latency, fails over automatically, and tells you which model answered and what it cost.</p><div class="actions"><a class="button primary" href="/login">Get an API key</a><a class="button" href="/models-page">See {len(CATALOGUE)} catalogued systems</a><a class="button" href="/docs">Read the docs</a></div></section>
<section class="grid"><div class="card"><h3>Routed by published scores</h3><p class="muted"><code>auto</code> follows one public rule: best benchmark score among healthy models, or the cheapest, fastest or best-balanced if you ask. {len(live)} routes are live now.</p></div><div class="card"><h3>Two API shapes</h3><p class="muted">A decision API (<code>/v1/systemone</code>, <code>/v1/multimodal</code>) and an OpenAI-compatible <code>/v1/chat/completions</code> with typed <code>questions</code>.</p></div><div class="card"><h3>Prepaid, capped, transparent</h3><p class="muted">Per-decision prices from USD 0.01 per 1,000. Prepaid credit via Stripe, daily spend caps, failed calls never charged, unused credit refundable.</p></div></section>
<h2>One request</h2><pre>{esc(HOME_EXAMPLE)}</pre><p class="muted">The response contains the typed answers, the model that answered, any attempts it failed over from, and the cost in the <code>X-Jev-Cost-Usd</code> header.</p>'''
    return page("One API for open decision models", body, user)

@app.get("/models")
def models(): return {"object":"list","data":[{"id":k,**v} for k,v in public_models().items()],"listed_only":[{f:v.get(f) for f in ("id","display","vendor","homepage","terms_basis","terms_url","jevbench","imagejevbench")} for v in listed_models()],"routing_policy":ROUTING_POLICY}

def status_badge(s: str) -> str:
    cls={"live":"live","unknown":"live"}.get(s,"offline")
    label={"unknown":"live"}.get(s,s)
    return f'<span class="badge {cls}">{esc(label)}</span>'

def best_score(v: dict[str, Any]) -> float:
    return float((v.get("jevbench") or {}).get("score") or (v.get("imagejevbench") or {}).get("score") or 0)

@app.get("/models-page", response_class=HTMLResponse)
def models_page(request: Request):
    pm=public_models()
    concrete=sorted(((k,v) for k,v in pm.items() if k not in META_MODELS),key=lambda kv:(-best_score(kv[1]),kv[0]))
    rows="".join(f'<tr><th scope="row"><strong>{esc(k)}</strong><br><span class="muted">{esc(v.get("display",""))} · {esc(v["provider"])}</span></th><td>{status_badge(v["status"])}</td><td>{esc(", ".join(v["modalities"]))}<br><span class="muted">{esc(", ".join(v.get("question_types",[])))}</span></td><td>{esc(v["billing"])}</td><td>{bench_cell(v)}</td><td class="muted" style="font-size:13px">{esc(v["terms"])}</td></tr>' for k,v in concrete)
    listed="".join(f'<tr><th scope="row"><strong>{esc(v["display"])}</strong><br><span class="muted">{esc(v["vendor"])}</span></th><td>{bench_cell(v)}</td><td class="muted" style="font-size:13px">{esc(v["terms_basis"])}</td><td>'+(f'<a href="{esc(v["homepage"])}" rel="noopener">Provider ↗</a>' if v.get("homepage") else '')+'</td></tr>' for v in sorted(listed_models(),key=lambda v:(-best_score(v),v["display"])))
    metas="".join(f'<tr><th scope="row"><code>{esc(k)}</code></th><td>{status_badge(pm[k]["status"])}</td><td>{esc(v["terms"])}</td><td class="muted">{esc(" → ".join(pm[k].get("routing_order",[])[:6]) or "—")}</td></tr>' for k,v in META_MODELS.items())
    hosting=""
    if HOSTING_CONTROL_URL and HOSTING_CONTROL_TOKEN:
        hosted="".join(f'<tr><th scope="row"><strong>{esc(k)}</strong><br><span class="muted">{esc(v["gpu"])}</span></th><td>{esc(", ".join(v["modalities"]))}</td><td>USD {v["price_microusd_per_minute"]/1_000_000:.5f}/min</td><td>about {v["cold_start_seconds"]//60} min</td></tr>' for k,v in ON_DEMAND_MODELS.items())
        hosting='<h2>Dedicated GPUs on demand</h2><div class="table-wrap"><table><thead><tr><th>Model</th><th>Input</th><th>Price</th><th>Cold start</th></tr></thead><tbody>'+hosted+'</tbody></table></div>'
    body=f'''<h1 style="font-size:52px">Models</h1><p class="lead">Every Jev-class decision system we found reachable online, with the licence or terms basis on which we route to it, or why we do not. Scores are the published JevBench v1.4.2 and ImageJevBench v0.1 results from benchmarkheaven.com, measured independently and never through this router.</p>
<h2>Routing aliases</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>Status</th><th>Rule</th><th>Current order (text, choice)</th></tr></thead><tbody>{metas}</tbody></table></div>
<h2>Routable models</h2><div class="table-wrap"><table><thead><tr><th>Model</th><th>Status</th><th>Input · questions</th><th>Price</th><th>Published score</th><th>Why we may route</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Listed for comparison, not routed</h2><p class="muted">These systems are online, but their licence or terms do not let a third party route or resell traffic, or they are an author's free demo. Use them directly with their provider.</p><div class="table-wrap"><table><thead><tr><th>System</th><th>Published score</th><th>Why not routed</th><th></th></tr></thead><tbody>{listed}</tbody></table></div>{hosting}'''
    return page("Models", body, current_user(request))

@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    paid=sorted(((k,v) for k,v in MODELS.items() if v["routing"]=="paid" and k!="stripe-test-paid"),key=lambda kv:(kv[1]["price_per_1k_cents"],-best_score(kv[1])))
    rows="".join(f'<tr><th scope="row">{esc(k)}<br><span class="muted">{esc(v.get("display",""))}</span></th><td>USD {v["price_per_1k_cents"]/100:.2f}</td><td>USD {v["price_per_1k_cents"]*10:.2f}</td><td>{bench_cell(v)}</td></tr>' for k,v in paid) or '<tr><td colspan=4 class="muted">No paid models are configured right now.</td></tr>'
    free=", ".join(esc(k) for k,v in MODELS.items() if v["routing"]=="free") or "none"
    byok=", ".join(esc(k) for k,v in MODELS.items() if v["routing"]=="byok") or "none right now"
    body=f'''<h1 style="font-size:52px">Pricing</h1><p class="lead">Pay per decision from prepaid credit. No subscription, no monthly minimum. One decision is one request, however many questions it contains.</p>
<div class="table-wrap"><table><thead><tr><th>Model we host</th><th>Per 1,000 decisions</th><th>Per 1,000,000 decisions</th><th>Published score</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="grid" style="margin-top:24px"><div class="card"><h3>Free routes</h3><p class="muted">{free}. Third-party services we route to at no charge, within their published limits.</p></div><div class="card"><h3>Bring your own key</h3><p class="muted">{byok}. Your key goes straight to that provider; we add nothing and never store it.</p></div><div class="card"><h3>Credit</h3><p class="muted">Top up USD {MIN_TOPUP_CENTS/100:.0f} to USD {MAX_TOPUP_CENTS/100:.0f} per payment by card through Stripe. Prices in USD; VAT is added where it applies. Credit does not expire. Unused credit is refundable within 14 days of purchase.</p></div></div>
<h2>Limits</h2><p>{RATE_LIMIT_PER_MINUTE} requests per minute per API key, a daily spend cap you set yourself (default USD 100), and at most USD {DAILY_TOPUP_CAP_CENTS/100:.0f} of top-ups per account per day. Failed and failed-over attempts are never charged. <code>auto</code> requests cost the price of the model that actually answered, shown in every response.</p>'''
    return page("Pricing", body, current_user(request))

@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    body=f'''<h1 style="font-size:52px">API docs</h1><p class="lead">Typed decisions over text and images. Authenticate with an API key from the dashboard: <code>Authorization: Bearer jvr_...</code></p>
<h2>Decision API</h2><p><code>POST /v1/systemone</code> (text) and <code>POST /v1/multimodal</code> (up to {MAX_IMAGES} PNG/JPEG/WebP data URLs, 2 MB each). Body: <code>state</code> (text or JSON), <code>questions</code> (up to 32), optional <code>model</code> (default <code>auto</code>), <code>fallback</code>, <code>route</code>, <code>provider_keys</code>.</p>
<ul><li><code>choice</code>: <code>criteria</code> is an object of options; answer has <code>choice</code>, <code>confidence</code> and, where the model provides them, <code>probabilities</code>.</li><li><code>noul</code>: yes/no; answer <code>noul</code> is the probability of yes (0 to 1).</li><li><code>score</code>: <code>criteria</code> is an ordered list of levels; answer <code>score</code> is the index of the chosen level.</li></ul>
<pre>{esc(DOC_EXAMPLE)}</pre><h3>Response</h3><pre>{esc(RESPONSE_EXAMPLE)}</pre>
<h2>Images</h2><pre>{esc(IMAGE_EXAMPLE)}</pre>
<h2>OpenAI-compatible</h2><p><code>POST /v1/chat/completions</code> and <code>GET /v1/models</code>. Put typed questions in the extra top-level <code>questions</code> field; the chat messages become the state. The assistant message content is the JSON answers object, and the full decision is in <code>decision</code>. You can also send a complete decision request as JSON in the last user message.</p><pre>{esc(OPENAI_EXAMPLE)}</pre>
<h2 id="routing">Routing</h2><p>{esc(ROUTING_POLICY)}</p><ul><li><code>route.prefer</code>: <code>quality</code>, <code>balanced</code>, <code>price</code> or <code>latency</code></li><li><code>route.task</code>: <code>general</code>, <code>classification</code>, <code>judgement</code>, <code>hard</code> or <code>easy</code>; ranks by that JevBench tier's accuracy.</li><li><code>route.max_price_per_1k</code> (USD), <code>route.max_latency_ms</code>, <code>route.benchmarked_only</code></li><li><code>fallback</code>: extra model IDs tried after the first choice.</li></ul><p>Health: every model is checked in the background, and a circuit breaker removes a model for 60 seconds after two consecutive failures. Every response names the answering model (<code>X-Jev-Model</code>), its latency and its cost.</p>
<h2>Bring your own key</h2><p>For providers marked BYOK, pass <code>"provider_keys": {{"vendor": "key"}}</code>. The key is sent only to that provider for this request and is never stored or logged. We charge nothing for these calls.</p>
<h2>Errors</h2><p><code>401</code> bad key · <code>402</code> insufficient credit or your daily cap · <code>429</code> rate limit · <code>502</code> every attempted model failed (nothing charged) · <code>503</code> no eligible model, or the operator's daily cap.</p>
<p class="notice">TypeSafe's Jev is not available through Jev Router; use it directly from TypeSafe AI.</p>'''
    return page("API docs", body, current_user(request))

LEGAL_UPDATED="26 September 2026"
OPERATOR="productivity-boost.com Betriebs UG (haftungsbeschränkt) &amp; Co. KG, Reichenbergerstr. 2, 94036 Passau, Germany"
LEGAL={
"terms":("Terms of service",f"""<p>These terms govern your use of Jev Router (jev-router.com). By creating an account or calling the API you agree to them.</p>
<h3>1. Who you contract with</h3><p>Jev Router is operated by {OPERATOR}, registered at Amtsgericht Passau under HRB 8453, VAT ID DE296812612 ("we"). Full details are in the <a href="/impressum">Impressum</a>.</p>
<h3>2. The service</h3><p>Jev Router forwards typed decision requests (choice, yes/no, score) to decision models: models we host ourselves, third-party services whose terms allow it, and, if you supply your own key, providers you have an account with. The model list, prices and routing rule are published on the <a href="/models-page">models</a>, <a href="/pricing">pricing</a> and <a href="/docs#routing">docs</a> pages. Model outputs are probabilistic and can be wrong. You are responsible for how you use them, in particular for decisions with legal or similarly significant effects on people, which need human review.</p>
<h3>3. Accounts and keys</h3><p>You sign in with Google, must be at least 18, and must be entitled to accept these terms for any organisation you act for. You are responsible for your API keys and all usage made with them. Do not put keys into client-side code.</p>
<h3>4. Acceptable use</h3><p>No unlawful content; no attempts to overload, probe or circumvent limits; no use of the service to attack third parties; no resale of access without our written consent; no personal data you have no lawful basis to process. You must respect the terms of any provider you reach with your own key.</p>
<h3>5. Credit and payment</h3><p>The service runs on prepaid credit bought through Stripe. Usage is charged per decision at the price shown for the model that answered. Credit does not expire, has no cash value and is not transferable. We never charge you automatically. Prices are in US dollars; VAT is added where it applies. Refunds: see the <a href="/refunds">refund policy</a>.</p>
<h3>6. Availability and limits</h3><p>The service is provided without a service level agreement. Models can be unavailable; the router then fails over or returns an error without charging. We may enforce rate, spend, top-up and abuse limits, and may change the model list and prices; price changes never apply retroactively.</p>
<h3>7. Suspension</h3><p>We may suspend accounts that breach these terms or endanger the service or third parties; where possible we warn you first. Unused credit of a suspended account is refunded unless the suspension is due to fraud.</p>
<h3>8. Liability</h3><p>We are liable without limitation for intent and gross negligence, for injury to life, body or health, and under the German Product Liability Act. For ordinary negligence we are liable only for breach of essential contractual obligations, limited to the foreseeable damage typical for this contract. Otherwise our liability is excluded. For businesses, total liability per 12 months is limited to the amount paid to us in that period.</p>
<h3>9. Law and changes</h3><p>German law applies, excluding the UN Sales Convention; for consumers, the mandatory law of their country of residence remains applicable. For businesses, the courts at our registered seat have exclusive jurisdiction. We announce material changes to these terms by email 30 days in advance.</p>
<h3>10. Trademarks</h3><p>Jev is a trademark of TypeSafe AI, Inc. Jev Router is independent, is not affiliated with, endorsed by or sponsored by TypeSafe AI, and does not provide access to TypeSafe's Jev. Other model names belong to their respective owners.</p>
<p>Contact: info@productivity-boost.com</p>"""),
"privacy":("Privacy policy",f"""<p>Controller (Art. 4(7) GDPR): {OPERATOR}, represented by Florian Standhartinger, info@productivity-boost.com. Supervisory authority: Bayerisches Landesamt für Datenschutzaufsicht (BayLDA), Ansbach.</p>
<h3>What we store</h3><p><strong>Account:</strong> Google account identifier, email address and name from Google sign-in (scopes openid, email, profile). <strong>API keys:</strong> a prefix and a hash, never the key itself. <strong>Billing:</strong> credit ledger and Stripe checkout and payment references; card data is handled only by Stripe. <strong>Usage:</strong> per request the time, model, provider, price and latency. Legal basis: performance of the contract (Art. 6(1)(b) GDPR) and legal retention duties (Art. 6(1)(c)).</p>
<h3>Request content</h3><p>We do not store request bodies, answers or images. Their content is forwarded only to the model that handles the request: our own servers in Germany for models we host, or the third-party provider named on the models page when that model is selected directly or chosen by <code>auto</code>. Use the <code>model</code> and <code>route</code> settings to restrict which providers receive your data. Provider keys you pass for bring-your-own-key calls are forwarded to that provider and never stored.</p>
<h3>Logs and cookies</h3><p>Server logs contain the request path, status and duration. IP addresses are processed for rate limiting and security (Art. 6(1)(f)). We use one strictly necessary session cookie for sign-in, and no tracking or advertising cookies.</p>
<h3>Processors</h3><p>Stripe (payments), Google (sign-in), Hetzner (hosting in Germany), Neon (database). Stripe and Google may process data outside the EU under standard contractual clauses.</p>
<h3>Retention and rights</h3><p>Account data is kept while your account exists; accounting records for the periods required by German commercial and tax law. You have the rights of access, rectification, erasure, restriction, portability and objection, and may complain to a supervisory authority. Write to info@productivity-boost.com.</p>"""),
"refunds":("Refund policy","""<p>Unused prepaid credit can be refunded in full within 14 days of purchase: email info@productivity-boost.com from your account address with the date of the top-up. After 14 days, unused credit is still refunded on request if we discontinue the service or raise prices to your disadvantage.</p><p>Credit already used for decisions is not refundable, because the service has been delivered. If you are a consumer in the EU you have a statutory 14-day right of withdrawal; by starting to use the credit you ask us to begin the service immediately, and the withdrawal then covers only the unused part.</p><p>Refunds go to the original payment method through Stripe, and the corresponding credit is removed from your balance. Failed requests are never charged in the first place.</p>"""),
"impressum":("Impressum","""<p>Information according to § 5 DDG</p><p><strong>productivity-boost.com Betriebs UG (haftungsbeschränkt) &amp; Co. KG</strong><br>Reichenbergerstr. 2<br>94036 Passau<br>Germany</p><p>Represented by Florian Standhartinger</p><p>Email: info@productivity-boost.com<br>Telephone: +49 178 1981631</p><p>Register court: Amtsgericht Passau, register number HRB 8453</p><p>VAT ID according to § 27a UStG: DE296812612</p><p>Responsible for content according to § 18 (2) MStV: Florian Standhartinger, address as above.</p><p>EU online dispute resolution: <a href="https://ec.europa.eu/consumers/odr" rel="noopener">ec.europa.eu/consumers/odr</a>. We are neither obliged nor willing to take part in dispute resolution proceedings before a consumer arbitration board.</p>""")}

for _slug,(_title,_text) in LEGAL.items():
    def make_legal(slug=_slug,title=_title,text=_text):
        @app.get("/"+slug, response_class=HTMLResponse, name="legal_"+slug)
        def legal(request: Request): return page(title, f'<h1 style="font-size:52px">{title}</h1><div class="card">{text}<p class="muted">Last updated {LEGAL_UPDATED}.</p></div>', current_user(request))
    make_legal()

@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    pm=public_models()
    concrete=[(k,v) for k,v in pm.items() if k not in META_MODELS]
    rows="".join(f'<tr><th scope="row">{esc(k)}</th><td>{status_badge(v["status"])}</td><td>{(str(round(v["observed_latency_ms"]))+" ms") if v.get("observed_latency_ms") else "—"}</td></tr>' for k,v in concrete)
    live=len(live_routes())
    return page("Status", f'<h1 style="font-size:52px">Status</h1><div class="card"><h3><span class="live">●</span> Gateway operational</h3><p class="muted">{live} of {len(concrete)} routes currently report live. Machine-readable: <a href="/models"><code>/models</code></a>.</p></div><div class="table-wrap"><table><thead><tr><th>Model</th><th>Health</th><th>Observed latency (moving average)</th></tr></thead><tbody>{rows}</tbody></table></div>', current_user(request))

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
        idle_setting=db.execute("SELECT idle_minutes FROM hosting_settings WHERE user_id=?",(u["id"],)).fetchone()
        instances=db.execute("SELECT * FROM hosting_instances WHERE user_id=? ORDER BY started_at DESC LIMIT 20",(u["id"],)).fetchall()
    cap=settings["daily_spend_cap_microusd"] if settings else 100_000_000
    idle=idle_setting["idle_minutes"] if idle_setting else 10
    token=csrf(request)
    checkout_nonce=secrets.token_urlsafe(18); request.session["checkout_nonce"]=checkout_nonce
    keyrows="".join(f'<tr><td>{esc(k["name"])}</td><td><code>{esc(k["prefix"])}…</code></td><td>' + ('revoked' if k["revoked_at"] else f'<form method="post" action="/api-keys/{k["id"]}/revoke"><input type="hidden" name="csrf" value="{token}"><button>Revoke</button></form>') + '</td></tr>' for k in keys) or '<tr><td colspan=3 class="muted">No API keys yet.</td></tr>'
    usagerows="".join(f'<tr><td>{esc(x["model"])}</td><td>{x["n"]}</td><td>${x["microusd"]/1_000_000:.6f}</td></tr>' for x in usage) or '<tr><td colspan=3 class="muted">No usage yet.</td></tr>'
    def instance_action(x):
        if x["status"] not in ("starting","running","stopping"): return esc(x["stop_reason"] or "stopped")
        return f'<form method="post" action="/hosting/{esc(x["id"])}/stop"><input type="hidden" name="csrf" value="{token}"><button>Stop now</button></form>'
    instance_rows="".join(f'<tr><td>{esc(x["model"])}</td><td>{esc(x["status"])}</td><td><span class="host-meter" data-start="{esc(x["started_at"])}" data-rate="{int(x["price_microusd_per_minute"])}" data-billed="{int(x["billed_minutes"])}">USD {int(x["billed_minutes"])*int(x["price_microusd_per_minute"])/1_000_000:.5f}</span></td><td>{instance_action(x)}</td></tr>' for x in instances) or '<tr><td colspan=4 class="muted">No dedicated instances yet.</td></tr>'
    hosting_ready=bool(HOSTING_CONTROL_URL and HOSTING_CONTROL_TOKEN)
    start_cards="".join(f'<div class="card"><h3>{esc(k)}</h3><p class="muted">{esc(v["gpu"])} · about {v["cold_start_seconds"]//60} min cold start</p><p>USD {v["price_microusd_per_minute"]/1_000_000:.5f}/min<br><span class="muted">provider cost + {v["margin_percent"]}% margin</span></p><form method="post" action="/hosting/start"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="model" value="{esc(k)}"><button class="primary" {"" if hosting_ready else "disabled"}>{"Start dedicated GPU" if hosting_ready else "Temporarily unavailable"}</button></form></div>' for k,v in ON_DEMAND_MODELS.items())
    hosting_html=(f'''<div class="grid"><div class="card"><h3>Hosting controls</h3><form method="post" action="/hosting/settings"><input type="hidden" name="csrf" value="{token}"><label>Idle shutdown (2–60 minutes)<input name="idle_minutes" type="number" min="2" max="60" value="{idle}"></label><button>Save</button></form><p class="muted">Balance zero always stops the instance.</p></div></div><h2>Start an open decision model</h2><div class="grid">{start_cards}</div><h2>Dedicated instances</h2><div class="table-wrap"><table><tr><th>Model</th><th>Status</th><th>Live hosted-time meter</th><th>State</th></tr>{instance_rows}</table></div>''') if hosting_ready else ""
    return page("Dashboard",f'''<h1 style="font-size:52px">Dashboard</h1><div class="grid"><div class="card"><span class="muted">Prepaid balance</span><h2>USD {balance(u["id"])/1_000_000:.2f}</h2><form method="post" action="/billing/checkout"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="checkout_nonce" value="{checkout_nonce}"><label>Top up (USD cents, minimum {MIN_TOPUP_CENTS})<input name="amount_cents" type="number" min="{MIN_TOPUP_CENTS}" value="{MIN_TOPUP_CENTS}"></label><button class="primary" type="submit" {"" if PAYMENTS_ENABLED else "disabled"}>{"Add credit with Stripe" if STRIPE_MODE=="live" else "Checkout in Stripe test mode"}</button><p class="muted" style="font-size:13px">Unused credit is refundable within 14 days. By paying you ask us to start the service immediately. <a href="/terms">Terms</a> · <a href="/refunds">Refunds</a></p></form></div><div class="card"><h3>New API key</h3><form method="post" action="/api-keys"><input type="hidden" name="csrf" value="{token}"><label>Name<input name="name" maxlength="60" value="Default"></label><button type="submit">Create key</button></form></div><div class="card"><h3>Daily spend cap</h3><form method="post" action="/settings/spend-cap"><input type="hidden" name="csrf" value="{token}"><label>USD per day<input name="cap_usd" type="number" min="0" max="1000" step="0.01" value="{cap/1_000_000:.2f}"></label><button>Save</button></form><p class="muted">Requests beyond this cap return 402.</p></div></div>{hosting_html}<h2>API keys</h2><div class="table-wrap"><table><tr><th>Name</th><th>Key</th><th>Status</th></tr>{keyrows}</table></div><h2>Shared-route usage</h2><div class="table-wrap"><table><tr><th>Model</th><th>Decisions</th><th>Charged</th></tr>{usagerows}</table></div><script>function meters(){{document.querySelectorAll('.host-meter').forEach(function(e){{var s=Date.parse(e.dataset.start),r=Number(e.dataset.rate),b=Number(e.dataset.billed),m=Math.max(b,Math.ceil((Date.now()-s)/60000));e.textContent='USD '+(m*r/1000000).toFixed(5)+' · '+m+' started min';}})}}meters();setInterval(meters,1000);</script>''',u)

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
    with dbconn() as db:
        rows=db.execute("SELECT created_at,model,provider,microusd,latency_ms FROM usage_events WHERE user_id=? ORDER BY id DESC LIMIT 200",(u["id"],)).fetchall()
        credits=db.execute("SELECT created_at,kind,microusd,ref FROM credit_events WHERE user_id=? ORDER BY id DESC LIMIT 200",(u["id"],)).fetchall()
    body="".join(f'<tr><td>{esc(r["created_at"][:19])}</td><td>{esc(r["model"])}</td><td>{esc(r["provider"])}</td><td>${r["microusd"]/1_000_000:.6f}</td><td>{r["latency_ms"]:.0f} ms</td></tr>' for r in rows) or '<tr><td colspan=5>No usage yet.</td></tr>'
    ledger="".join(f'<tr><td>{esc(r["created_at"][:19])}</td><td>{esc(r["kind"])}</td><td>{r["microusd"]/1_000_000:+.6f} USD</td><td><code>{esc(r["ref"])}</code></td></tr>' for r in credits) or '<tr><td colspan=4>No credit entries yet.</td></tr>'
    return page("Usage",f'<h1 style="font-size:52px">Usage</h1><p class="lead">Balance: ${balance(u["id"])/1_000_000:.4f}</p><div class="table-wrap"><table><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Model</th><th scope="col">Provider</th><th scope="col">Charged</th><th scope="col">Latency</th></tr></thead><tbody>{body}</tbody></table></div><h2>Credit ledger</h2><div class="table-wrap"><table><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Entry</th><th scope="col">Amount</th><th scope="col">Reference</th></tr></thead><tbody>{ledger}</tbody></table></div>',u)

@app.post("/billing/checkout")
async def checkout(request:Request):
    u=require_user(request); form=await request.form(); check_csrf(request,form.get("csrf")); amount=int(form.get("amount_cents",0))
    nonce=str(form.get("checkout_nonce", ""))
    if not nonce or not hmac.compare_digest(nonce,request.session.get("checkout_nonce","")): raise HTTPException(409,"Stale checkout form; reload the dashboard")
    if amount<MIN_TOPUP_CENTS or amount>MAX_TOPUP_CENTS: raise HTTPException(400,f"Top-up must be {MIN_TOPUP_CENTS}–{MAX_TOPUP_CENTS} cents")
    if not PAYMENTS_ENABLED: raise HTTPException(503,"Payments are currently disabled")
    if not STRIPE_SECRET_KEY: raise HTTPException(503,"Stripe is not configured")
    hour_ago=datetime.fromtimestamp(time.time()-3600,timezone.utc).isoformat(); day_start=datetime.now(timezone.utc).date().isoformat()
    with dbconn() as db:
        recent=int(db.execute("SELECT COUNT(*) n FROM stripe_checkout_intents WHERE user_id=? AND created_at>=?",(u["id"],hour_ago)).fetchone()["n"])
        paid_today=int(db.execute("SELECT COALESCE(SUM(amount_cents),0) n FROM stripe_checkout_intents WHERE user_id=? AND status='paid' AND created_at>=?",(u["id"],day_start)).fetchone()["n"])
    if recent>=CHECKOUTS_PER_HOUR: raise HTTPException(429,"Too many checkout attempts; try again in an hour",headers={"Retry-After":"3600"})
    if paid_today+amount>DAILY_TOPUP_CAP_CENTS: raise HTTPException(400,f"Daily top-up limit is USD {DAILY_TOPUP_CAP_CENTS/100:.0f}; contact us for more")
    data=[("mode","payment"),("success_url",APP_URL+"/dashboard?payment=success"),("cancel_url",APP_URL+"/dashboard?payment=cancelled"),("customer_email",u["email"]),("client_reference_id",str(u["id"])),("metadata[user_id]",str(u["id"])),("metadata[credits_cents]",str(amount)),("line_items[0][price_data][currency]","usd"),("line_items[0][price_data][unit_amount]",str(amount)),("line_items[0][price_data][product_data][name]","Jev Router prepaid API credit"),("line_items[0][quantity]","1"),("payment_intent_data[receipt_email]",u["email"]),("automatic_tax[enabled]","true" if STRIPE_AUTOMATIC_TAX else "false"),("metadata[app]","jev-router"),("payment_intent_data[metadata][app]","jev-router"),("payment_intent_data[metadata][user_id]",str(u["id"]))]
    async with httpx.AsyncClient(timeout=20) as client: resp=await client.post("https://api.stripe.com/v1/checkout/sessions",data=dict(data),auth=httpx.BasicAuth(STRIPE_SECRET_KEY,""),headers={"Idempotency-Key":f"topup-{u['id']}-{amount}-{nonce}"})
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
    # The Stripe account is shared with other products; only our own checkouts carry app=jev-router.
    if event["type"].startswith("checkout.session.") and (obj.get("metadata") or {}).get("app")!="jev-router":
        return {"received":True,"ignored":"not a jev-router checkout"}
    try:
        return process_stripe_event(event,obj)
    except HTTPException: raise
    except Exception:
        # A concurrent delivery of the same event loses the unique-key race; report it as a duplicate.
        with dbconn() as db:
            if db.execute("SELECT 1 FROM stripe_events WHERE id=?",(event["id"],)).fetchone(): return {"received":True,"duplicate":True}
        raise

def process_stripe_event(event:dict,obj:dict):
    with dbconn() as db:
        if db.execute("SELECT 1 FROM stripe_events WHERE id=?",(event["id"],)).fetchone(): return {"received":True,"duplicate":True}
        if event["type"]=="checkout.session.completed" and obj.get("payment_status")=="paid":
            try: uid=int(obj["metadata"]["user_id"]); cents=int(obj["metadata"]["credits_cents"])
            except Exception: raise HTTPException(400,"Stripe checkout has no top-up metadata")
            # Every credit must match a checkout this app created, in this mode, for this user and amount.
            expected_livemode=STRIPE_MODE=="live"
            intent=db.execute("SELECT * FROM stripe_checkout_intents WHERE session_id=?",(obj.get("id"),)).fetchone()
            charged=int((obj.get("amount_subtotal") if STRIPE_AUTOMATIC_TAX else obj.get("amount_total")) or 0)
            valid=(
                intent is not None and bool(obj.get("livemode"))==expected_livemode
                and obj.get("currency")==intent["currency"]
                and charged==int(intent["amount_cents"])
                and cents==int(intent["amount_cents"])
                and uid==int(intent["user_id"])
                and str(obj.get("client_reference_id"))==str(intent["user_id"])
                and intent["status"]!="paid"
            )
            if not valid: raise HTTPException(400,"Stripe checkout does not match a pending top-up")
            db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,cents*10_000,"stripe_topup",obj["id"],now_iso()))
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

ROUTE_KEYS=("model","fallback","route","provider_keys")

def validate_body(body:dict,multimodal:bool):
    if not isinstance(body,dict): raise HTTPException(400,"Request body must be an object")
    if "state" not in body: raise HTTPException(400,"state is required")
    questions=body.get("questions")
    if not isinstance(questions,dict) or not questions: raise HTTPException(400,"questions must be a non-empty object")
    if len(questions)>32: raise HTTPException(400,"At most 32 questions per request")
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
    route=body.get("route") or {}
    if not isinstance(route,dict): raise HTTPException(400,"route must be an object")
    if route.get("prefer",routing.PREFERENCES[0]) not in routing.PREFERENCES: raise HTTPException(400,{"error":"invalid_route_prefer","options":list(routing.PREFERENCES)})
    if route.get("task","general") not in routing.TASKS: raise HTTPException(400,{"error":"invalid_route_task","options":list(routing.TASKS)})
    for limit in ("max_price_per_1k","max_latency_ms"):
        if limit in route and (not isinstance(route[limit],(int,float)) or route[limit]<0): raise HTTPException(400,f"route.{limit} must be a non-negative number")
    provider_keys=body.get("provider_keys") or {}
    if not isinstance(provider_keys,dict) or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>512 for k,v in provider_keys.items()): raise HTTPException(400,"provider_keys must map vendor names to key strings")
    choices=([body["model"]] if body.get("model") else ["auto"])+fallback
    if len(choices)!=len(set(choices)): raise HTTPException(400,"Duplicate models in route")
    listed={v["id"] for v in listed_models()}
    blocked=[m for m in choices if m in listed]
    if blocked: raise HTTPException(400,{"error":"model_not_routable","models":blocked,"detail":"Listed for comparison only; its provider's terms do not allow routing through us. Call that provider directly."})
    unknown=[m for m in choices if m not in MODELS and m not in META_MODELS]
    if unknown: raise HTTPException(400,{"error":"unknown_model","models":unknown,"options":list(META_MODELS)+list(MODELS)})
    images=body.get("images") or []
    if multimodal and not images: raise HTTPException(400,"images are required")
    if not multimodal and images: raise HTTPException(400,"Use /v1/multimodal for image requests")
    qtypes={q["type"] for q in questions.values()}
    expanded=[]
    for model in choices:
        if model in META_MODELS:
            routed=route_candidates(model,image=bool(images),question_types=qtypes,provider_keys=provider_keys,route=route)
            expanded.extend(routed)
        else:
            info=MODELS[model]
            if not qtypes<=set(info["question_types"]): raise HTTPException(400,{"error":"unsupported_question_type","model":model,"supported":info["question_types"]})
            if info["routing"]=="byok" and CATALOGUE[model]["byok_vendor"] not in provider_keys: raise HTTPException(400,{"error":"provider_key_required","model":model,"vendor":CATALOGUE[model]["byok_vendor"]})
            expanded.append(model)
    choices=list(dict.fromkeys(expanded))
    if not choices: raise HTTPException(503,{"error":"no_eligible_model","detail":"No healthy model supports this modality, these question types and route limits right now."})
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

def hosting_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + HOSTING_CONTROL_TOKEN} if HOSTING_CONTROL_TOKEN else {}

async def hosting_control(method: str, path: str, payload: dict | None = None) -> Any:
    if not HOSTING_CONTROL_URL or not HOSTING_CONTROL_TOKEN:
        raise HTTPException(503, "On-demand provisioning is temporarily unavailable")
    async with httpx.AsyncClient(timeout=120) as client:
        response=await client.request(method, HOSTING_CONTROL_URL+path, json=payload, headers=hosting_headers())
        response.raise_for_status()
        return response.json() if response.content else {}

def charge_hosting_minute(instance_id: str, minute_number: int) -> bool:
    today=datetime.now(timezone.utc).date().isoformat()
    with dbconn() as db:
        row=db.execute("SELECT * FROM hosting_instances WHERE id=?",(instance_id,)).fetchone()
        if not row:return False
        if DATABASE_URL: db.execute("SELECT pg_advisory_xact_lock(?)",(9_876_544,)); db.execute("SELECT pg_advisory_xact_lock(?)",(row["user_id"],))
        else: db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM hosting_instances WHERE id=?",(instance_id,)).fetchone()
        if not row or row["status"] not in ("starting","running"): return False
        if db.execute("SELECT 1 FROM hosting_minute_events WHERE instance_id=? AND minute_number=?",(instance_id,minute_number)).fetchone(): return True
        price=int(row["price_microusd_per_minute"]); provider_cost=int(row["provider_microusd_per_minute"]); uid=int(row["user_id"])
        available=int(db.execute("SELECT COALESCE(SUM(microusd),0) n FROM credit_events WHERE user_id=?",(uid,)).fetchone()["n"])
        global_spend=int(db.execute("SELECT COALESCE(SUM(microusd),0) n FROM usage_events WHERE created_at>=?",(today,)).fetchone()["n"])
        user_spend=int(db.execute("SELECT COALESCE(SUM(microusd),0) n FROM usage_events WHERE user_id=? AND created_at>=?",(uid,today)).fetchone()["n"])
        provider_spend=int(db.execute("SELECT COALESCE(SUM(provider_microusd),0) n FROM hosting_minute_events WHERE created_at>=?",(today,)).fetchone()["n"])
        caprow=db.execute("SELECT daily_spend_cap_microusd FROM account_settings WHERE user_id=?",(uid,)).fetchone(); user_cap=int(caprow["daily_spend_cap_microusd"]) if caprow else 100_000_000
        if available<price or global_spend+price>OPERATOR_DAILY_CAP_CENTS*10_000 or user_spend+price>user_cap or provider_spend+provider_cost>HOSTING_DAILY_PROVIDER_CAP_MICROUSD: return False
        ref=f"hosting:{instance_id}:{minute_number}"
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?)",(uid,-price,"hosting_minute",ref,now_iso()))
        usage_id=db.execute("INSERT INTO usage_events(user_id,api_key_id,model,microusd,provider,latency_ms,created_at) VALUES(?,NULL,?,?,?,?,?) RETURNING id",(uid,row["model"],price,"hosting:"+row["provider"],0,now_iso())).fetchone()["id"]
        db.execute("INSERT INTO hosting_minute_events(instance_id,minute_number,user_id,price_microusd,provider_microusd,usage_event_id,created_at) VALUES(?,?,?,?,?,?,?)",(instance_id,minute_number,uid,price,provider_cost,usage_id,now_iso()))
        db.execute("UPDATE hosting_instances SET billed_minutes=?,last_billed_at=? WHERE id=? AND status IN ('starting','running')",(minute_number,now_iso(),instance_id))
    return True

def refund_hosting_minute(instance_id: str, minute_number: int, reason: str) -> None:
    with dbconn() as db:
        row=db.execute("SELECT * FROM hosting_minute_events WHERE instance_id=? AND minute_number=?",(instance_id,minute_number)).fetchone()
        if not row:return
        ref=f"hosting-refund:{instance_id}:{minute_number}"
        db.execute("INSERT INTO credit_events(user_id,microusd,kind,ref,created_at) VALUES(?,?,?,?,?) ON CONFLICT(ref) DO NOTHING",(row["user_id"],row["price_microusd"],reason,ref,now_iso()))
        db.execute("DELETE FROM usage_events WHERE id=?",(row["usage_event_id"],))
        db.execute("DELETE FROM hosting_minute_events WHERE instance_id=? AND minute_number=?",(instance_id,minute_number))
        db.execute("UPDATE hosting_instances SET billed_minutes=(SELECT COUNT(*) FROM hosting_minute_events WHERE instance_id=?) WHERE id=?",(instance_id,instance_id))

async def stop_hosting_instance(instance: Any, reason: str) -> None:
    with dbconn() as db:
        current=db.execute("SELECT * FROM hosting_instances WHERE id=?",(instance["id"],)).fetchone()
        if not current or current["status"]=="stopped":return
        if DATABASE_URL: db.execute("SELECT pg_advisory_xact_lock(?)",(9_876_544,)); db.execute("SELECT pg_advisory_xact_lock(?)",(current["user_id"],))
        else: db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT * FROM hosting_instances WHERE id=?",(instance["id"],)).fetchone()
        if not current or current["status"]=="stopped":return
        db.execute("UPDATE hosting_instances SET status='stopping',stop_reason=? WHERE id=?",(reason,instance["id"]))
    provider_id=current["provider_instance_id"]
    if provider_id:
        await hosting_control("DELETE", "/instances/"+urllib.parse.quote(provider_id, safe=""))
    with dbconn() as db:
        db.execute("UPDATE hosting_instances SET status='stopped',stopped_at=?,stop_reason=? WHERE id=? AND status='stopping'",(now_iso(),reason,instance["id"]))

async def reconcile_hosting() -> dict[str, int]:
    """Meter whole hosted minutes and verify every instance against the provider."""
    charged=stopped=orphans=0
    now=datetime.now(timezone.utc); today=now.date().isoformat()
    with dbconn() as db: rows=db.execute("SELECT * FROM hosting_instances WHERE status IN ('starting','running','stopping')").fetchall()
    try: remote=(await hosting_control("GET","/instances")).get("instances",[])
    except Exception as exc:
        print(f"hosting reaper provider verification failed: {type(exc).__name__}",flush=True)
        return {"charged_minutes":0,"stopped":0,"orphans_removed":0,"provider_verified":False}
    remote_by_id={str(x.get("id")):x for x in remote if x.get("id")}
    # Provisioning can complete while the provider inventory request is in flight.
    # Re-read provider IDs so a newly persisted instance is never reaped as orphaned.
    with dbconn() as db:
        current_rows=db.execute("SELECT id,provider_instance_id FROM hosting_instances WHERE status IN ('starting','running','stopping')").fetchall()
    known={str(x["provider_instance_id"]) for x in current_rows if x["provider_instance_id"]}
    active_owners={str(x["id"]) for x in current_rows}
    for external in remote:
        is_unowned=(not external.get("owner") or str(external.get("owner")) not in active_owners)
        if external.get("managed_by") == "jev-router" and str(external.get("id")) not in known and is_unowned:
            try:
                await hosting_control("DELETE","/instances/"+urllib.parse.quote(str(external["id"]),safe="")); orphans+=1
            except Exception: pass
    for row in rows:
        if row["status"]=="stopping":
            provider_id=str(row["provider_instance_id"] or "")
            if not provider_id or provider_id not in remote_by_id:
                with dbconn() as db: db.execute("UPDATE hosting_instances SET status='stopped',stopped_at=?,stop_reason=COALESCE(stop_reason,'provider shutdown verified') WHERE id=? AND status='stopping'",(now_iso(),row["id"]))
                stopped+=1; continue
            try: await stop_hosting_instance(row,row["stop_reason"] or "retrying provider shutdown")
            except Exception as exc: print(f"hosting shutdown retry failed: {type(exc).__name__}",flush=True)
            else: stopped+=1
            continue
        provider_id=str(row["provider_instance_id"] or "")
        if provider_id and provider_id not in remote_by_id:
            with dbconn() as db: db.execute("UPDATE hosting_instances SET status='stopped',stopped_at=?,stop_reason='provider instance missing' WHERE id=?",(now_iso(),row["id"]))
            stopped+=1; continue
        idle=int((now-datetime.fromisoformat(row["last_used_at"])).total_seconds()//60)
        with dbconn() as db: setting=db.execute("SELECT idle_minutes FROM hosting_settings WHERE user_id=?",(row["user_id"],)).fetchone()
        idle_limit=int(setting["idle_minutes"]) if setting else 10
        if idle>=idle_limit and row["status"] in ("starting","running"):
            try: await stop_hosting_instance(row,f"idle for {idle_limit} minutes")
            except Exception as exc: print(f"hosting idle stop failed: {type(exc).__name__}",flush=True)
            stopped+=1; continue
        elapsed=max(1,int((now-datetime.fromisoformat(row["started_at"])).total_seconds()//60)+1)
        for minute in range(int(row["billed_minutes"])+1,elapsed+1):
            if charge_hosting_minute(row["id"],minute): charged+=1
            else:
                try: await stop_hosting_instance(row,"prepaid balance or spend cap exhausted")
                except Exception as exc: print(f"hosting zero-balance stop failed: {type(exc).__name__}",flush=True)
                stopped+=1; break
    return {"charged_minutes":charged,"stopped":stopped,"orphans_removed":orphans,"provider_verified":True}

async def hosting_reaper_loop() -> None:
    while True:
        try: await reconcile_hosting()
        except asyncio.CancelledError: raise
        except Exception as exc: print(f"hosting reaper failed: {type(exc).__name__}",flush=True)
        await asyncio.sleep(HOSTING_REAPER_SECONDS)

def normalise_answers(model:str,questions:dict,out:Any)->dict:
    """Check a provider reply against the request so a malformed answer counts as a failure."""
    answers=(out or {}).get("answers") if isinstance(out,dict) else None
    if not isinstance(answers,dict): raise ValueError("provider returned no answers object")
    for name,q in questions.items():
        a=answers.get(name)
        if not isinstance(a,dict): raise ValueError(f"provider omitted answer {name}")
        a.setdefault("type",q["type"])
        if q["type"]=="choice" and a.get("choice") not in q["criteria"]: raise ValueError(f"provider returned an invalid choice for {name}")
        if q["type"]=="noul":
            value=a.get("noul")
            if not isinstance(value,(int,float)) or not 0<=value<=1: raise ValueError(f"provider returned an invalid noul for {name}")
        if q["type"]=="score":
            value=a.get("score")
            if not isinstance(value,(int,float)) or not 0<=value<len(q["criteria"]): raise ValueError(f"provider returned an invalid score for {name}")
    return {"answers":{name:answers[name] for name in questions},"usage":out.get("usage",{})}

async def call_model(model:str,body:dict,request:Request):
    payload={k:v for k,v in body.items() if k not in ROUTE_KEYS}
    questions=body.get("questions") or {}
    if model=="stripe-test-paid":
        if STRIPE_MODE != "test" or not PAYMENTS_ENABLED:
            raise ValueError("test billing route is disabled")
        answers={}
        for name,q in questions.items():
            if q.get("type")=="noul": answers[name]={"type":"noul","noul":0.75}
            elif q.get("type")=="choice":
                option=next(iter(q["criteria"]))
                answers[name]={"type":"choice","choice":option,"confidence":1.0}
            else: answers[name]={"type":"score","score":0}
        return {"model":model,"answers":answers,"usage":{"estimated_cost_usd":0.0001,"test_mode":True}}
    entry=CATALOGUE[model]
    if entry["adapter"]=="classifier.dev":
        answers={}
        async with httpx.AsyncClient(timeout=15) as client:
            for name,q in questions.items():
                labels=["yes","no"] if q.get("type")=="noul" else list((q.get("criteria") or {}).keys())
                if len(labels)<2: raise ValueError("question requires at least two criteria")
                prompt=str(body.get("state"))+("\n\n"+q["instructions"] if q.get("instructions") else "")
                r=await client.post("https://classifier.dev/v1/classify",json={"inputs":[prompt],"labels":labels,"tier":"fast"}); r.raise_for_status(); item=r.json()["results"][0]; scores=item.get("scores",{})
                answers[name]={"type":q["type"],"noul":float(scores.get("yes",0))} if q["type"]=="noul" else {"type":q["type"],"choice":item["label"],"confidence":item.get("confidence"),"probabilities":scores}
        return normalise_answers(model,questions,{"answers":answers,"usage":{"estimated_cost_usd":0}})
    endpoint=entry_endpoint(entry)
    if not endpoint: raise ValueError("endpoint is not configured")
    headers={}
    if entry.get("token_env") and os.getenv(entry["token_env"]): headers["Authorization"]="Bearer "+os.environ[entry["token_env"]]
    if entry["routing"]=="byok":
        key=(body.get("provider_keys") or {}).get(entry["byok_vendor"])
        if not key: raise ValueError("provider key missing")
        headers[entry.get("byok_header","Authorization")]=(entry.get("byok_prefix","Bearer "))+key
    if entry["adapter"]=="systemone":
        out=await post_json(endpoint+entry.get("path","/v1/systemone"),payload,headers,timeout=entry.get("timeout_s",30))
        return normalise_answers(model,questions,out)
    raise ValueError(f"unknown adapter {entry['adapter']}")

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

async def decide(request:Request,multimodal=False,body:dict|None=None):
    if int(request.headers.get("content-length","0") or 0)>MAX_BODY: raise HTTPException(413,"Request too large")
    user=api_user(request.headers.get("authorization")); enforce_rate_limit(user["key_id"])
    if body is None:
        try: body=await request.json()
        except Exception: raise HTTPException(400,"Request body must be JSON")
    choices=validate_body(body,multimodal); errors=[]
    for model in choices[:MAX_ATTEMPTS]:
        info=MODELS[model]; microusd=int(info["price_per_1k_cents"]*10)  # cents/1000 -> micro-USD/decision
        reservation=secrets.token_hex(16); reserve_credit(user["id"],microusd,reservation)
        started=time.perf_counter()
        try:
            out=await call_model(model,body,request); ms=round((time.perf_counter()-started)*1000,1)
            out["model"]=model
            HEALTH.ok(model,ms)
            with dbconn() as db:
                db.execute("INSERT INTO usage_events(user_id,api_key_id,model,microusd,provider,latency_ms,created_at) VALUES(?,?,?,?,?,?,?)",(user["id"],user["key_id"],model,microusd,info["provider"],ms,now_iso()))
                db.execute("DELETE FROM spend_reservations WHERE ref=?",(reservation,))
            out["routing"]={"requested":body.get("model") or "auto","answered_by":model,"attempts":errors,"price_usd_per_1k":info["price_per_1k_cents"]/100,"billing":info["routing"]}
            return JSONResponse(out,headers={"X-Jev-Provider":info["provider"],"X-Jev-Model":model,"X-Jev-Latency-Ms":str(ms),"X-Jev-Cost-Usd":f"{microusd/1_000_000:.6f}","Cache-Control":"no-store"})
        except HTTPException: raise
        except asyncio.CancelledError:
            refund_reservation(user["id"],microusd,reservation); raise
        except Exception as e:
            HEALTH.fail(model,f"{type(e).__name__}")
            refund_reservation(user["id"],microusd,reservation)
            detail=f"HTTP {e.response.status_code}" if isinstance(e,httpx.HTTPStatusError) else type(e).__name__ if not isinstance(e,ValueError) else str(e)[:160]
            errors.append({"model":model,"error":detail})
    raise HTTPException(502,{"error":"all_providers_failed","attempts":errors})

@app.post("/v1/systemone")
async def systemone(request:Request): return await decide(request,False)

@app.post("/v1/multimodal")
async def multimodal(request:Request): return await decide(request,True)

def chat_to_native(body:dict)->dict:
    messages=body.get("messages") or []
    if not isinstance(messages,list) or not messages: raise HTTPException(400,"messages required")
    if body.get("questions") is not None:
        # Extension: plain chat messages become the state; typed questions ride alongside.
        parts=[]
        for m in messages:
            content=m.get("content") if isinstance(m,dict) else None
            if isinstance(content,list): content=" ".join(str(p.get("text","")) for p in content if isinstance(p,dict) and p.get("type")=="text")
            if content: parts.append(f'{m.get("role","user")}: {content}')
        native={"state":"\n".join(parts),"questions":body["questions"]}
        if body.get("images"): native["images"]=body["images"]
    else:
        try: native=json.loads(messages[-1]["content"])
        except Exception: raise HTTPException(400,"Send typed questions in the top-level 'questions' field, or make the final message a JSON System One request")
        if not isinstance(native,dict): raise HTTPException(400,"Final message must be a JSON object")
    native["model"]=body.get("model") or "auto"
    for key in ("fallback","route","provider_keys"):
        if body.get(key) is not None: native[key]=body[key]
    return native

@app.post("/v1/chat/completions")
async def chat(request:Request):
    try: body=await request.json()
    except Exception: raise HTTPException(400,"Request body must be JSON")
    if not isinstance(body,dict): raise HTTPException(400,"Request body must be an object")
    native=chat_to_native(body)
    response=await decide(request,bool(native.get("images")),native); data=json.loads(response.body)
    return JSONResponse({"id":"dec-"+secrets.token_hex(8),"object":"chat.completion","created":int(time.time()),"model":response.headers.get("X-Jev-Model"),"choices":[{"index":0,"message":{"role":"assistant","content":json.dumps({"answers":data["answers"]})},"finish_reason":"stop"}],"decision":data,"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}},headers={k:v for k,v in response.headers.items() if k.lower().startswith("x-jev-")})

@app.get("/v1/models")
def openai_models():
    return {"object":"list","data":[{"id":k,"object":"model","created":1790000000,"owned_by":v["provider"],"status":v["status"],"billing":v["billing"]} for k,v in public_models().items()]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","8080")))
