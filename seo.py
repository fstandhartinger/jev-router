"""Search and link-preview metadata for jev-router.com.

Kept apart from app.py so the gateway code stays about the gateway. app.py
calls ``head()`` from its page template and ``register()`` once at import.

Rules for everything in here: say only what the running service does, and
never print a benchmark number that is not read from the live model catalogue
(the catalogue itself links to Benchmark Heaven, where the numbers come from).
"""

from __future__ import annotations

import html
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

STATIC = Path(__file__).with_name("static")
SITE = "https://jev-router.com"
OG_IMAGE = "/og.png"
OG_ALT = "Jev Router: Jev-class decision models through one API, with transparent routing and per-decision prices."
DEFAULT_DESCRIPTION = ("Call Jev-class decision models through one API: typed answers with "
                       "probabilities, transparent score-ordered routing and per-decision prices.")
NON_AFFILIATION = ("Jev is a trademark of TypeSafe AI, Inc. Jev Router is an independent service and is "
                   "not affiliated with, endorsed by, or sponsored by TypeSafe AI. We do not provide access "
                   "to TypeSafe's Jev model.")

#: Per-path description and whether the page is worth indexing. Paths not listed
#: fall back to the default description; signed-in pages are noindex.
DESCRIPTIONS: dict[str, str] = {
    "/": DEFAULT_DESCRIPTION,
    "/models-page": "Every decision model on Jev Router with its live status, input types, "
                    "price per 1,000 decisions and published benchmark track.",
    "/docs": "API docs for Jev Router: the decision and OpenAI-compatible endpoints, auto routing "
             "with price and latency preferences, curl and Python examples.",
    "/status": "Live gateway status and how many Jev Router routes currently report healthy.",
    "/decision-model-api": "A decision model API returns typed judgments with probabilities "
                           "instead of free text. What that is, when to use it, and a working "
                           "request against Jev Router.",
    "/jev-alternatives": "Jev alternatives you can call today: the Jev-class decision models on "
                         "Jev Router with their live status, price, licence and published "
                         "JevBench Score.",
    "/pricing": "Jev Router pricing: what each decision route costs per 1,000 decisions, "
                "bring-your-own-key routes, and how prepaid credit works.",
    "/system-one-models": "System One models make fast, typed decisions for software, the way "
                          "System 1 thinking does for people. What they are, how they differ "
                          "from chat LLMs, and how to call open ones.",
    "/terms": "Jev Router terms of service.",
    "/privacy": "What Jev Router stores, what it does not store, and who receives request content.",
    "/refunds": "Refund policy for Jev Router prepaid credit.",
    "/impressum": "Impressum and provider identification for Jev Router.",
}
#: The request path, so app.py's page() needs no extra argument at each call site.
CURRENT_PATH: ContextVar[str] = ContextVar("seo_current_path", default="/")
NOINDEX = {"/dashboard", "/usage", "/login", "/logout"}
SITEMAP = ["/", "/models-page", "/pricing", "/docs", "/decision-model-api", "/jev-alternatives",
           "/system-one-models", "/status", "/terms", "/privacy", "/refunds", "/impressum"]


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _ld(data: dict) -> str:
    # "</" can never close the script element early.
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def _faq_ld(faq: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}


def faq_html(faq: list[tuple[str, str]]) -> str:
    items = "".join(f'<div class="card"><h3>{_e(q)}</h3><p class="muted">{_e(a)}</p></div>' for q, a in faq)
    return f'<h2>Questions</h2><div class="grid">{items}</div>'


HOME_FAQ = [
    ("What is Jev Router?",
     "A prepaid gateway for Jev-class decision models: open models we host and third-party "
     "services whose terms allow it. You send state and typed questions "
     "to one endpoint and get typed answers back, from a concrete model you name or from a "
     "transparent meta route."),
    ("Is Jev Router open source?",
     "The gateway source is public on GitHub (fstandhartinger/jev-router) under the MIT licence. "
     "Each model's provider and terms are listed in the model catalogue."),
    ("Does Jev Router offer TypeSafe's Jev?", NON_AFFILIATION),
    ("How does the auto route pick a model?",
     "It ranks the healthy models that support your request's input type and question types by "
     "their published JevBench or ImageJevBench score, or by price, latency or a published balance "
     "of the three if you ask. The next model is the automatic fallback, and the model that "
     "answered and its price are returned with every response."),
]
PAGE_FAQ: dict[str, list[tuple[str, str]]] = {
    "/": HOME_FAQ,
    "/decision-model-api": [
        ("What is a decision model?",
         "A model that answers a fixed set of typed questions about some state (choice, yes/no, "
         "number) and returns a probability for each option, instead of generating free text."),
        ("When is a decision model API better than a chat LLM?",
         "When your code needs a judgment it can branch on: routing, moderation, triage, "
         "extraction into fixed fields, or checking another model's answer. The output is "
         "already typed, so there is no prompt-and-parse step."),
        ("Which endpoint do I call?",
         "POST /v1/systemone for text and POST /v1/multimodal for text with images, with an API "
         "key from your Jev Router dashboard."),
    ],
    "/jev-alternatives": [
        ("What counts as a Jev-class model?",
         "A decision model with the same request shape as Jev: state plus typed questions in, "
         "typed answers with probabilities out."),
        ("Where do the benchmark scores come from?",
         "From JevBench on Benchmark Heaven (benchmarkheaven.com/jev-models). Jev Router shows the "
         "published score and track next to each model and does not run its own leaderboard."),
        ("Does Jev Router offer TypeSafe's Jev?", NON_AFFILIATION),
    ],
    "/system-one-models": [
        ("What is a System One model?",
         "A small, fast model that makes one bounded judgment and returns it as data, named after "
         "the fast, intuitive System 1 in Kahneman's two-system picture of thinking."),
        ("How is that different from a reasoning LLM?",
         "A reasoning model (System 2) writes out steps and free text; a System One model answers "
         "a typed question directly, so it is cheaper and faster per call and easy to combine in code."),
    ],
}


def head(title: str, description: str | None = None, path: str | None = None) -> str:
    """Canonical, robots, Open Graph, Twitter and JSON-LD tags for one page."""
    path = path or CURRENT_PATH.get()
    desc = description or DESCRIPTIONS.get(path, DEFAULT_DESCRIPTION)
    url = SITE + ("" if path == "/" else path)
    full = f"{title} · Jev Router"
    tags = [
        f'<meta name="description" content="{_e(desc)}">',
        f'<link rel="canonical" href="{_e(url)}">',
        '<meta name="robots" content="noindex">' if path in NOINDEX else "",
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Jev Router">',
        f'<meta property="og:title" content="{_e(full)}">',
        f'<meta property="og:description" content="{_e(desc)}">',
        f'<meta property="og:url" content="{_e(url)}">',
        f'<meta property="og:image" content="{SITE}{OG_IMAGE}">',
        '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{_e(OG_ALT)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{_e(full)}">',
        f'<meta name="twitter:description" content="{_e(desc)}">',
        f'<meta name="twitter:image" content="{SITE}{OG_IMAGE}">',
    ]
    if path == "/":
        tags.append(_ld({"@context": "https://schema.org", "@type": "SoftwareApplication",
                         "name": "Jev Router", "url": SITE, "applicationCategory": "DeveloperApplication",
                         "operatingSystem": "Any (HTTP API)", "description": desc,
                         "sameAs": ["https://github.com/fstandhartinger/jev-router"],
                         "publisher": {"@type": "Organization",
                                       "name": "productivity-boost.com Betriebs UG (haftungsbeschränkt) & Co. KG"}}))
    if path in PAGE_FAQ:
        tags.append(_ld(_faq_ld(PAGE_FAQ[path])))
    return "".join(t for t in tags if t)


def register(app: FastAPI, page: Callable[..., HTMLResponse], models: Callable[[], dict[str, dict[str, Any]]],
             current_user: Callable[[Request], Any]) -> None:
    """Add robots.txt, sitemap.xml, llms.txt, the preview image and the intent pages."""

    @app.middleware("http")
    async def remember_path(request: Request, call_next):
        token = CURRENT_PATH.set(request.url.path)
        try:
            return await call_next(request)
        finally:
            CURRENT_PATH.reset(token)

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        disallow = "".join(f"Disallow: {p}\n" for p in sorted(NOINDEX | {"/hosting/", "/v1/", "/api-keys", "/billing/", "/webhooks/", "/auth/"}))
        return PlainTextResponse(f"User-agent: *\nAllow: /\n{disallow}\nSitemap: {SITE}/sitemap.xml\n")

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        urls = "".join(f"<url><loc>{SITE}{'' if p == '/' else p}</loc></url>" for p in SITEMAP)
        return Response('<?xml version="1.0" encoding="UTF-8"?>'
                        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                        media_type="application/xml")

    @app.get("/llms.txt", include_in_schema=False)
    def llms():
        lines = ["# Jev Router", "",
                 "> " + DEFAULT_DESCRIPTION, "",
                 NON_AFFILIATION, "",
                 "## Docs",
                 f"- [API docs]({SITE}/docs): endpoints, meta routes, curl and Python examples",
                 f"- [Model catalogue, JSON]({SITE}/models): live status, price and benchmark track per model",
                 f"- [Model catalogue, HTML]({SITE}/models-page)",
                 f"- [Decision model API]({SITE}/decision-model-api): what a decision model is and when to use one",
                 f"- [Jev alternatives]({SITE}/jev-alternatives): each model with licence and published JevBench Score",
                 f"- [System One models]({SITE}/system-one-models)",
                 "", "## Models (live catalogue at the time of this request)"]
        for key, info in models().items():
            if key.startswith("stripe-test") or "fixture" in str(info.get("adapter", "")):
                continue
            lines.append(f"- {key}: {info.get('status')}; {', '.join(info.get('modalities', []))}; {info.get('billing')}")
        lines += ["", "## Optional",
                  "- [Source code](https://github.com/fstandhartinger/jev-router)",
                  "- [JevBench scores on Benchmark Heaven](https://benchmarkheaven.com/jev-models)"]
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/og.png", include_in_schema=False)
    def og_image():
        return FileResponse(STATIC / "og.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    def models_table() -> str:
        rows = []
        for key, info in models().items():
            # Meta routes are not models; test fixtures are not for the public.
            if key in ("auto", "jev-class", "image-jev-class") or "fixture" in str(info.get("adapter", "")) \
                    or "test fixture" in str(info.get("provider", "")).lower() or key.startswith("stripe-test"):
                continue
            j, i = info.get("jevbench"), info.get("imagejevbench")
            # The headline number Benchmark Heaven publishes is the JevBench Score ("composite"), not a sub-score.
            if j and j.get("composite") is not None:
                bench = f'{float(j["composite"]):.1f}' + (f' (#{_e(j["rank"])})' if j.get("rank") else "") + f' · JevBench {_e(j.get("version", ""))}'
            elif i and i.get("score") is not None:
                bench = f'{float(i["score"]):.1f}' + (f' (#{_e(i["rank"])})' if i.get("rank") else "") + f' · ImageJevBench {_e(i.get("version", ""))}'
            else:
                bench = '<span class="muted">not benchmarked</span>'
            rows.append(f'<tr><th scope="row"><strong>{_e(key)}</strong><br><span class="muted">{_e(info.get("provider", ""))}</span></th>'
                        f'<td><span class="badge {_e(info.get("status", ""))}">{_e(info.get("status", ""))}</span></td>'
                        f'<td>{_e(", ".join(info.get("modalities", [])))}</td><td>{_e(info.get("billing", ""))}</td>'
                        f'<td>{_e(info.get("licence") or "not stated")}</td><td>{bench}</td></tr>')
        return ('<div class="table-wrap"><table><thead><tr><th>Model</th><th>Status</th><th>Input</th><th>Price</th>'
                '<th>Licence</th><th>JevBench Score</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')

    example = ('curl https://jev-router.com/v1/systemone \\\n  -H "Authorization: Bearer jvr_…" \\\n'
               '  -H "Content-Type: application/json" \\\n'
               '  -d \'{"model":"auto","state":"Ticket: I was charged twice this month.",'
               '"questions":{"team":{"type":"choice","instructions":"Which team handles this?",'
               '"criteria":{"billing":null,"tech":null,"sales":null}}}}\'')

    @app.get("/decision-model-api", response_class=HTMLResponse)
    def decision_model_api(request: Request):
        body = (f'<h1 style="font-size:52px">Decision model API</h1><p class="lead">Ask typed questions about some '
                f'state and get typed answers with probabilities back — no prompt-and-parse step.</p>'
                f'<h2>What a decision model does</h2><p>A chat model writes text you then have to parse. A decision '
                f'model gets the state (a ticket, a message, a record, an image) plus a set of questions with fixed '
                f'answer types, and returns each answer as data with a probability per option. Your code branches '
                f'on the result directly.</p><h2>One request</h2><pre>{_e(example)}</pre><p>The response names the '
                f'model that answered (also in the <code>X-Jev-Model</code> header) and the price that applied. '
                f'Name a concrete model to pin it, or use <code>auto</code> to let the published routing rule '
                f'pick the best healthy one.</p><h2>Good fits</h2><div class="grid">'
                f'<div class="card"><h3>Routing and triage</h3><p class="muted">Send a ticket, message or task to '
                f'the right queue, model or team.</p></div><div class="card"><h3>Checks</h3><p class="muted">Ask '
                f'whether another model\'s answer is complete, on-topic or safe before you use it.</p></div>'
                f'<div class="card"><h3>Extraction</h3><p class="muted">Fill fixed fields from free text or an '
                f'image, with a confidence you can threshold.</p></div></div>'
                f'<p class="actions"><a class="button primary" href="/docs">Read the API docs</a>'
                f'<a class="button" href="/models-page">See the models</a></p>'
                + faq_html(PAGE_FAQ["/decision-model-api"]))
        return page("Decision model API", body, current_user(request))

    @app.get("/jev-alternatives", response_class=HTMLResponse)
    def jev_alternatives(request: Request):
        body = ('<h1 style="font-size:52px">Jev alternatives</h1><p class="lead">Jev-class decision models '
                'you can call through one API today — open models and third-party services — with their '
                'live status, price and licence.</p>'
                f'<p class="notice">{_e(NON_AFFILIATION)}</p>'
                '<h2>Models on Jev Router</h2>' + models_table() +
                '<p class="muted">Status is live health, not a promise. The score is the published JevBench Score '
                '(or ImageJevBench score) and rank; see <a href="https://benchmarkheaven.com/jev-models"><u>JevBench on '
                'Benchmark Heaven</u></a> for the full comparison, method and every other system.</p>'
                '<h2>Choosing one</h2><p>Use <code>auto</code> if you want the best healthy model by '
                'published score with automatic fallback. Pin a concrete model if you need a stable price, a '
                'specific licence, or image input.</p>'
                '<p class="actions"><a class="button primary" href="/docs">Call a model</a>'
                '<a class="button" href="/decision-model-api">What is a decision model API?</a></p>'
                + faq_html(PAGE_FAQ["/jev-alternatives"]))
        return page("Jev alternatives", body, current_user(request))

    @app.get("/system-one-models", response_class=HTMLResponse)
    def system_one_models(request: Request):
        body = ('<h1 style="font-size:52px">System One models</h1><p class="lead">Small, fast models that make one '
                'bounded judgment and return it as data.</p><h2>The idea</h2><p>In the two-system picture of '
                'thinking, System 1 is fast and intuitive and System 2 is slow and deliberate. Reasoning LLMs are '
                'the System 2 of software. A System One model is the other half: it looks at some state and answers '
                'typed questions about it at once, with a probability for each option.</p><h2>Why it matters in '
                'code</h2><p>Because the answer is typed, you can combine many small judgments — route, rank, '
                'verify, extract — the way you combine function calls, and you only pay for a reasoning model where '
                'a judgment is not enough.</p><h2>Call one</h2><p>Jev Router serves open Jev-class System One models '
                'at <code>POST /v1/systemone</code>.</p><pre>' + _e(example) + '</pre>'
                '<p class="actions"><a class="button primary" href="/jev-alternatives">See the open models</a>'
                '<a class="button" href="/docs">API docs</a></p>' + faq_html(PAGE_FAQ["/system-one-models"]))
        return page("System One models", body, current_user(request))
