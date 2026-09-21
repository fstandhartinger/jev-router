import asyncio, os, time
from collections import defaultdict, deque
from datetime import date
from typing import Any
import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Jev Router", version="0.1.0")
MODELS = {
 "classifier-fast":{"provider":"classifier.dev","status":"live","score":84.8,"cost":"$0 within limits","terms":"Permitted within published limits; outputs may be used."},
 "semif-qwen3.5-4b":{"provider":"self-hosted","status":"offline","score":74.7,"cost":"infrastructure pass-through","terms":"Open weights; serverless endpoint not switched on."},
 "jev-latest":{"provider":"TypeSafe","status":"disabled","score":75.4,"cost":"provider price; no markup","terms":"Disabled: TypeSafe MCA forbids offering it as a standalone service."},
 "djev":{"provider":"Maisa","status":"disabled","score":74.3,"cost":"provider price; no markup","terms":"Disabled pending explicit proxy permission; weights are not released."},
 "simplejev-demo":{"provider":"Featherless","status":"disabled","score":None,"cost":"demo","terms":"Disabled: demo directs production users to a developer account; no proxy permission found."},
}
WINDOW=int(os.getenv("RATE_WINDOW_SECONDS","60")); IP_LIMIT=int(os.getenv("IP_RATE_LIMIT","60")); KEY_LIMIT=int(os.getenv("KEY_RATE_LIMIT","120"))
DAILY_USD=float(os.getenv("DAILY_BUDGET_USD","0")); seen=defaultdict(deque); spent={"day":date.today(),"usd":0.0}; lock=asyncio.Lock()
class Payload(BaseModel):
 state: Any
 questions: dict[str,dict[str,Any]]
 model: str|None=None
 fallback: list[str]=Field(default_factory=list)
def check_bucket(name,limit):
 now=time.monotonic(); q=seen[name]
 while q and q[0]<now-WINDOW: q.popleft()
 if len(q)>=limit: raise HTTPException(429,"rate limit exceeded",headers={"Retry-After":str(WINDOW)})
 q.append(now)
def labels(q):
 typ=q.get("type"); criteria=q.get("criteria")
 if typ=="noul": return ["yes","no"]
 if isinstance(criteria,dict): return list(criteria)
 if isinstance(criteria,list): return [str(x) for x in range(len(criteria))]
 raise HTTPException(400,"choice/score question requires criteria")
async def classifier(state,questions):
 answers={}; total_tokens=0
 async with httpx.AsyncClient(timeout=15) as client:
  for name,q in questions.items():
   ls=labels(q); prompt=str(state)+("\n\n"+str(q.get("instructions")) if q.get("instructions") else "")
   r=await client.post("https://classifier.dev/v1/classify",json={"inputs":[prompt],"labels":ls,"tier":"fast"})
   r.raise_for_status(); d=r.json(); item=d["results"][0]; scores=item.get("scores",{})
   typ=q.get("type")
   if typ=="noul": answers[name]={"type":"noul","noul":float(scores.get("yes",0))}
   elif typ=="choice": answers[name]={"type":"choice","choice":item["label"],"confidence":item.get("confidence"),"probabilities":scores}
   else: answers[name]={"type":"score","score":int(item["label"]),"probabilities":scores}
   total_tokens+=int((d.get("usage") or {}).get("input_tokens",0))
 return {"model":"classifier-fast","answers":answers,"usage":{"input_tokens":total_tokens,"estimated_cost_usd":0}}
@app.middleware("http")
async def guards(req,call_next):
 ip=(req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "unknown")).split(",")[0]
 key=req.headers.get("x-router-key","public")[:128]
 check_bucket("ip:"+ip,IP_LIMIT); check_bucket("key:"+key,KEY_LIMIT)
 return await call_next(req)
@app.get("/health")
def health(): return {"ok":True}
@app.get("/models")
def models(): return {"object":"list","data":[{"id":k,**v} for k,v in MODELS.items()],"neutrality":"No default model. Scores are informational JevBench results."}
@app.post("/v1/systemone")
async def systemone(body:Payload,response:Response,authorization:str|None=Header(default=None)):
 choices=([body.model] if body.model else [])+body.fallback
 if not choices: raise HTTPException(400,{"error":"model_required","options":list(MODELS)})
 errors=[]
 for model in choices:
  started=time.perf_counter()
  try:
   if model=="classifier-fast": out=await classifier(body.state,body.questions)
   elif model=="semif-qwen3.5-4b" and os.getenv("SEMIF_ENDPOINT"):
    async with httpx.AsyncClient(timeout=30) as c:
     rr=await c.post(os.environ["SEMIF_ENDPOINT"].rstrip("/")+"/v1/systemone",json=body.model_dump(),headers={"authorization":authorization} if authorization else {})
     rr.raise_for_status(); out=rr.json()
   else: raise RuntimeError(MODELS.get(model,{}).get("terms","unknown model"))
   ms=round((time.perf_counter()-started)*1000,1); cost=float((out.get("usage") or {}).get("estimated_cost_usd",0))
   async with lock:
    if spent["day"]!=date.today(): spent.update(day=date.today(),usd=0.0)
    if cost and spent["usd"]+cost>DAILY_USD: raise RuntimeError("daily operator budget exhausted")
    spent["usd"]+=cost
   response.headers.update({"X-Jev-Provider":MODELS[model]["provider"],"X-Jev-Model":model,"X-Jev-Latency-Ms":str(ms),"X-Jev-Cost-Usd":str(cost),"X-Jev-No-Markup":"true"})
   return out
  except Exception as e: errors.append({"model":model,"error":str(e)[:160]})
 raise HTTPException(502,{"error":"all_providers_failed","attempts":errors})
@app.get("/",response_class=HTMLResponse)
def home():
 return """<!doctype html><meta name=viewport content='width=device-width'><title>Jev Router</title><style>body{font:16px system-ui;max-width:760px;margin:60px auto;padding:0 20px;line-height:1.55}code,pre{background:#f3f3f3;padding:3px 6px}pre{padding:16px;overflow:auto}</style><h1>Jev Router</h1><p>One neutral, TypeSafe-compatible endpoint for Jev-class decision systems. Run by the authors of JevBench / Benchmark Heaven.</p><p><b>No model is selected for you.</b> Name a model or an explicit fallback list. Third-party calls carry no markup. Request content is not logged; operational metadata only.</p><pre>POST /v1/systemone
{"model":"classifier-fast","state":"...","questions":{"decision":{"type":"choice","instructions":"...","criteria":{"a":null,"b":null}}}}

# optional: "fallback":["semif-qwen3.5-4b"]</pre><p><a href=/models>Models, status, terms and JevBench scores</a> · <a href=/docs>API docs</a> · <a href=https://github.com/fstandhartinger/jev-router>MIT source</a></p><h2>Safeguards</h2><p>Per-IP and per-key limits, operator daily spend cap, bounded upstream timeouts, no content logs, explicit routing, and provider disclosure headers.</p><h2>Legal</h2><p><a href=/terms>Terms</a> · <a href=/impressum>Impressum & privacy</a></p>"""
@app.get("/terms",response_class=HTMLResponse)
def terms(): return "<h1>Terms</h1><p>Best-effort experimental service, no warranty or SLA. Lawful use only. Respect provider terms and limits. Do not use for prohibited consequential decisions. You retain your input rights. Requests may be sent to the explicitly selected provider. No resale markup is added.</p><p><a href=/>Home</a></p>"
@app.get("/impressum",response_class=HTMLResponse)
def impressum(): return "<h1>Impressum & privacy</h1><p>productivity-boost.com Betriebs UG (haftungsbeschränkt) &amp; Co. KG, Passau, Germany · VAT ID DE296812612.</p><p>No request content is logged or retained by this router. We process IP-derived rate-limit counters in memory and basic metadata for security and cost control. Selected upstream providers receive request content under their own policies.</p><p><a href=/>Home</a></p>"
