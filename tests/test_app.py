import subprocess,time,urllib.request,json
from unittest.mock import patch
import app
def test_live_contract():
 p=subprocess.Popen(["python3","app.py"],env={"PORT":"18080"})
 try:
  time.sleep(.2)
  assert json.load(urllib.request.urlopen("http://127.0.0.1:18080/health"))=={"ok":True}
  req=urllib.request.Request("http://127.0.0.1:18080/v1/systemone",data=b'{"state":"x","questions":{}}',headers={"content-type":"application/json"},method="POST")
  try: urllib.request.urlopen(req)
  except urllib.error.HTTPError as e: assert e.code==400 and b"model_required" in e.read()
 finally: p.terminate(); p.wait()

def test_djev_offline_without_endpoint():
 with patch.dict(app.os.environ, {}, clear=True):
  try: app.call_djev({"state":"x","questions":{}})
  except RuntimeError as error: assert "offline" in str(error)
  else: raise AssertionError("offline djev must refuse calls")
