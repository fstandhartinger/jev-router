from fastapi.testclient import TestClient
from app import app
c=TestClient(app)
def test_requires_model():
 r=c.post('/v1/systemone',json={'state':'x','questions':{}})
 assert r.status_code==400 and 'model_required' in r.text
def test_models_and_health():
 assert c.get('/health').json()=={'ok':True}
 assert any(x['id']=='classifier-fast' for x in c.get('/models').json()['data'])
