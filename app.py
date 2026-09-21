import json, os, time, urllib.request, urllib.error
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS={
 "classifier-fast":{"provider":"classifier.dev","status":"live","score":84.8,"cost":"$0 within limits","terms":"Permitted within published limits; outputs may be used."},
 "semif-qwen3.5-4b":{"provider":"self-hosted","status":"offline","score":74.7,"cost":"infrastructure pass-through","terms":"Open weights; serverless endpoint not switched on."},
 "jev-latest":{"provider":"TypeSafe","status":"disabled","score":75.4,"cost":"provider price; no markup","terms":"Disabled: TypeSafe MCA forbids offering it as a standalone service."},
 "djev":{"provider":"self-hosted djev-dev","status":"offline" if not os.getenv("DJEV_ENDPOINT") else "live","score":74.3,"cost":"infrastructure pass-through; H200 measured at $3.00/h","terms":"Apache-2.0 runtime over Google's Apache-2.0 DiffusionGemma weights; no djev-specific weights. DJEV_ENDPOINT activates it."},
 "simplejev-demo":{"provider":"Featherless","status":"disabled","score":None,"cost":"demo","terms":"Disabled: demo directs production users to a developer account; no proxy permission found."}}
seen=defaultdict(deque); window=int(os.getenv("RATE_WINDOW_SECONDS","60")); ip_limit=int(os.getenv("IP_RATE_LIMIT","60")); key_limit=int(os.getenv("KEY_RATE_LIMIT","120"))
def limited(bucket,limit):
 now=time.monotonic(); q=seen[bucket]
 while q and q[0]<now-window:q.popleft()
 if len(q)>=limit:return True
 q.append(now); return False
def call_classifier(state,questions):
 answers={}
 for name,q in questions.items():
  typ=q.get("type"); criteria=q.get("criteria")
  labels=["yes","no"] if typ=="noul" else list(criteria) if isinstance(criteria,dict) else [str(i) for i in range(len(criteria or []))]
  if len(labels)<2: raise ValueError("question requires at least two criteria")
  prompt=str(state)+(("\n\n"+str(q["instructions"])) if q.get("instructions") else "")
  data=json.dumps({"inputs":[prompt],"labels":labels,"tier":"fast"}).encode()
  req=urllib.request.Request("https://classifier.dev/v1/classify",data=data,headers={"content-type":"application/json"},method="POST")
  with urllib.request.urlopen(req,timeout=15) as r:d=json.load(r)
  item=d["results"][0]; scores=item.get("scores",{})
  if typ=="noul": answers[name]={"type":"noul","noul":float(scores.get("yes",0))}
  elif typ=="choice": answers[name]={"type":"choice","choice":item["label"],"confidence":item.get("confidence"),"probabilities":scores}
  else: answers[name]={"type":"score","score":int(item["label"]),"probabilities":scores}
 return {"model":"classifier-fast","answers":answers,"usage":{"estimated_cost_usd":0}}
def call_djev(body):
 endpoint=os.getenv("DJEV_ENDPOINT","").rstrip("/")
 if not endpoint: raise RuntimeError("self-hosted djev-dev is offline; DJEV_ENDPOINT is unset")
 payload={k:v for k,v in body.items() if k not in ("model","fallback")}
 data=json.dumps(payload).encode()
 headers={"content-type":"application/json"}
 if os.getenv("DJEV_API_KEY"): headers["authorization"]="Bearer "+os.environ["DJEV_API_KEY"]
 req=urllib.request.Request(endpoint+"/v1/request",data=data,headers=headers,method="POST")
 with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
HOME="""<!doctype html><meta name=viewport content='width=device-width'><title>Jev Router</title><style>body{font:16px system-ui;max-width:760px;margin:60px auto;padding:0 20px;line-height:1.55}pre{background:#f3f3f3;padding:16px;overflow:auto}</style><h1>Jev Router</h1><p>One neutral, TypeSafe-compatible endpoint for Jev-class decision systems. Run by the authors of JevBench / Benchmark Heaven.</p><p><b>No model is selected for you.</b> Name a model or explicit fallback list. No third-party markup. Request content is not logged.</p><pre>POST /v1/systemone
{"model":"classifier-fast","state":"...","questions":{"decision":{"type":"choice","instructions":"...","criteria":{"a":null,"b":null}}}}</pre><p><a href=/models>Models and status</a> · <a href=/docs>API docs</a> · <a href=https://github.com/fstandhartinger/jev-router>MIT source</a></p><p><a href=/terms>Terms</a> · <a href=/impressum>Impressum & privacy</a></p>"""
class H(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def send(self,status,body,headers=None,ctype="application/json"):
  raw=body.encode() if isinstance(body,str) else json.dumps(body).encode()
  self.send_response(status); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(raw)))
  for k,v in (headers or {}).items(): self.send_header(k,str(v))
  self.end_headers(); self.wfile.write(raw)
 def guard(self):
  ip=(self.headers.get("cf-connecting-ip") or self.client_address[0]).split(",")[0]; key=self.headers.get("x-router-key","public")[:128]
  if limited("ip:"+ip,ip_limit) or limited("key:"+key,key_limit): self.send(429,{"error":"rate_limit"},{"Retry-After":window}); return False
  return True
 def do_GET(self):
  if not self.guard(): return
  if self.path=="/health": return self.send(200,{"ok":True})
  if self.path=="/models": return self.send(200,{"object":"list","data":[{"id":k,**v} for k,v in MODELS.items()],"neutrality":"No default model. Scores are informational JevBench results."})
  if self.path=="/": return self.send(200,HOME,ctype="text/html; charset=utf-8")
  if self.path=="/terms": return self.send(200,"<h1>Terms</h1><p>Experimental, no warranty or SLA. Lawful use within provider limits. Selected providers receive request content. No resale markup.</p>",ctype="text/html")
  if self.path=="/impressum": return self.send(200,"<h1>Impressum & privacy</h1><p>productivity-boost.com Betriebs UG (haftungsbeschränkt) &amp; Co. KG, Passau, Germany · VAT ID DE296812612.</p><p>No request content is logged or retained by this router.</p>",ctype="text/html")
  if self.path=="/docs": return self.send(200,{"title":"Jev Router","endpoint":"POST /v1/systemone","request":{"model":"required","fallback":"optional ordered list","state":"any","questions":"TypeSafe question map"},"headers":["X-Jev-Provider","X-Jev-Model","X-Jev-Latency-Ms","X-Jev-Cost-Usd","X-Jev-No-Markup"]})
  self.send(404,{"error":"not_found"})
 def do_POST(self):
  if not self.guard(): return
  if self.path!="/v1/systemone": return self.send(404,{"error":"not_found"})
  try: body=json.loads(self.rfile.read(int(self.headers.get("content-length","0"))))
  except Exception: return self.send(400,{"error":"bad_json"})
  choices=(([body["model"]] if body.get("model") else [])+(body.get("fallback") or []))
  if not choices:return self.send(400,{"error":"model_required","options":list(MODELS)})
  errors=[]
  for model in choices:
   started=time.perf_counter()
   try:
    if model=="classifier-fast": out=call_classifier(body.get("state"),body.get("questions") or {}); provider="classifier.dev"; cost="0"
    elif model=="djev": out=call_djev(body); provider="self-hosted djev-dev"; cost="infrastructure-pass-through"
    else: raise RuntimeError(MODELS.get(model,{}).get("terms","unknown model"))
    ms=round((time.perf_counter()-started)*1000,1)
    return self.send(200,out,{"X-Jev-Provider":provider,"X-Jev-Model":model,"X-Jev-Latency-Ms":ms,"X-Jev-Cost-Usd":cost,"X-Jev-No-Markup":"true"})
   except Exception as e: errors.append({"model":model,"error":str(e)[:160]})
  self.send(502,{"error":"all_providers_failed","attempts":errors})
if __name__=="__main__": ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","8080"))),H).serve_forever()
