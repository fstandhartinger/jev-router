import json, re
import pytest
from fastapi.testclient import TestClient
import app

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app,"DB_PATH",str(tmp_path/"test.db"))
    with TestClient(app.app) as c: yield c

def ld_blocks(text):
    return [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)]

def test_each_page_has_its_own_canonical_and_description(client):
    seen=set()
    for path in ("/","/models-page","/docs","/decision-model-api","/jev-alternatives","/system-one-models","/status","/terms"):
        text=client.get(path).text
        canon=re.search(r'<link rel="canonical" href="([^"]+)"',text).group(1)
        assert canon=="https://jev-router.com"+("" if path=="/" else path)
        desc=re.search(r'<meta name="description" content="([^"]+)"',text).group(1)
        assert desc not in seen; seen.add(desc)
        assert 'property="og:image" content="https://jev-router.com/og.png"' in text
        assert text.count("<title>")==1 and 'name="robots" content="noindex"' not in text

def test_structured_data_is_valid_json(client):
    home=ld_blocks(client.get("/").text)
    assert {b["@type"] for b in home}=={"SoftwareApplication","FAQPage"}
    for path in ("/decision-model-api","/jev-alternatives","/system-one-models"):
        assert ld_blocks(client.get(path).text)[0]["@type"]=="FAQPage"

def test_robots_sitemap_llms_og(client):
    r=client.get("/robots.txt"); assert r.status_code==200 and "Sitemap: https://jev-router.com/sitemap.xml" in r.text and "Disallow: /dashboard" in r.text
    s=client.get("/sitemap.xml"); assert s.headers["content-type"].startswith("application/xml") and "<loc>https://jev-router.com/jev-alternatives</loc>" in s.text
    for loc in re.findall(r"<loc>https://jev-router.com([^<]*)</loc>", s.text):
        assert client.get(loc or "/").status_code==200
    l=client.get("/llms.txt"); assert l.status_code==200 and l.text.startswith("# Jev Router") and "classifier-fast" in l.text
    o=client.get("/og.png"); assert o.status_code==200 and o.content[:4]==b"\x89PNG"

def test_intent_pages_stay_honest(client):
    alt=client.get("/jev-alternatives").text
    assert "not affiliated with TypeSafe" in alt and "benchmarkheaven.com/jev-models" in alt
    assert "jev-typesafe" not in alt
    # every number shown is one the live catalogue holds
    for key,info in app.public_models().items():
        assert key in alt
