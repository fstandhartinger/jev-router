# Jev Router — implementation report (21 September 2026)

## Djev self-hosting correction (21 September 2026)

`Davipar/djev-dev` is Maisa engineer David Villalón's Apache-2.0 self-hostable Djev inference runtime over Google's Apache-2.0 `diffusiongemma-26B-A4B-it` checkpoint. It adds no model weights of its own. The earlier statement that djev could not be self-hosted was wrong.

The gateway now includes `djev` as a self-hosted provider. It is intentionally **offline** and costs $0 while `DJEV_ENDPOINT` is unset; setting that endpoint activates the route. The hosted Maisa API remains unproxied because no public proxy permission was established. No production GPU is running.

One H200 BF16 measurement with the stock runtime and an HTTP admission limit raised from 8 to 32 measured:

| concurrency | throughput | p50 | p95 |
|---:|---:|---:|---:|
| 1 | 24.8 decisions/s | 0.040 s | 0.041 s |
| 4 | 58.8 decisions/s | 0.067 s | 0.072 s |
| 16 | 110.1 decisions/s | 0.141 s | 0.168 s |

The BF16 checkpoint is 48.1 GiB on disk and the runtime reported about 49.2 GiB consumed before KV cache. It fits B200, H200, H100 80 GB, and RTX PRO 6000 96 GB. NVFP4 is optional on those cards, not required for fit; it is the practical route for smaller-memory Blackwell systems such as DGX Spark.

## Outcome

- Public API: https://jev-router.app.mintapis.com
- Public MIT source: https://github.com/fstandhartinger/jev-router
- Contract: POST /v1/systemone, matching TypeSafe's state/model/questions and answers shape.
- Discovery: GET /models; OpenAPI: /docs; health: /health.
- Routing is explicit. Omitting model returns HTTP 400 with the options. fallback is an ordered, caller-provided list; transport errors and timeouts move to the next entry.
- Successful responses carry X-Jev-Provider, X-Jev-Model, X-Jev-Latency-Ms, X-Jev-Cost-Usd, and X-Jev-No-Markup: true.
- The site states that Benchmark Heaven/JevBench's authors operate it. It has terms, privacy information, and the company Impressum.
- Safeguards: per-IP and per-caller-key in-memory rate limits, bounded upstream timeouts, zero operator spend by default, no request-body/access logging, and no stored upstream credentials.

## Providers and permissions

| Route | State | Terms finding |
|---|---|---|
| classifier-fast | **live** | classifier.dev's 19 Sep terms permit lawful use within its per-IP limits and say answers are the caller's to use. The gateway stays within those limits, charges nothing, and identifies the provider. |
| semif-qwen3.5-4b | **wired, offline** | Open implementation/weights. SEMIF_ENDPOINT activates a TypeSafe-compatible scale-to-zero endpoint; it is intentionally unset, so there is no GPU bill. |
| jev-latest | **disabled** | TypeSafe's current master customer agreement forbids making the service available as a standalone service. That rules out this hosted gateway even for a request-scoped user key. |
| djev | **disabled** | The preview API documents calls and pricing, but no public terms granting proxy/resale were found. It stays off pending written permission. |
| simplejev-demo | **disabled** | The public demo is explicitly limited and directs production use to a Featherless developer account. No proxy grant was found; the demo is not used as gateway capacity. |

The requested “every legitimate provider” rule therefore leaves classifier.dev live today. This is deliberately conservative: a public endpoint is not itself permission to re-publish the service behind another public endpoint.

## SemIf measurement and economics

The frozen SemIf build is Qwen3.5-4B BF16 with the author's direct option-logit scoring path. The prior exact-card JevBench run measured one request at a time at **p50 0.20 s / p95 0.32 s**. Its earlier capacity model, explicitly a projection, is **31,680 decisions/hour** at concurrency 4.

I attempted the required fresh 1/4/16 concurrency run on Lium. A one-GPU RTX A6000 rental at $0.42/hour remained PENDING and never provided SSH, so no request could run. I terminated it and verified Lium lists no pods. I did **not** invent concurrency results:

| concurrency | throughput | p50 | p95 | status |
|---:|---:|---:|---:|---|
| 1 | about 5 decisions/s from prior serial p50 | 0.20 s | 0.32 s | measured in prior frozen JevBench run |
| 4 | 8.8 decisions/s / 31,680 per hour | about 0.45 s | about 0.73 s | prior batching-based projection, not measured |
| 16 | unknown | unknown | unknown | fresh pod never became runnable |

This is the one incomplete measurement requirement. It is labeled, rather than converting a failed rental into fake data.

Current price observations and the projected 31,680 decisions/hour capacity:

| SemIf venue | $/GPU-hour | $/1,000 at 10% | at 30% | at 100% | 24/7 month |
|---|---:|---:|---:|---:|---:|
| RunPod on-demand A5000 | $0.27 | $0.0852 | $0.0284 | $0.0085 | $197.10 |
| RunPod on-demand L4 | $0.49 | $0.1547 | $0.0516 | $0.0155 | $357.70 |
| RunPod on-demand RTX 4090 | $0.74 | $0.2336 | $0.0779 | $0.0234 | $540.20 |
| RunPod serverless 24 GB (L4/A5000/3090 class) | $0.69 active | $0.0218 per 1,000 active-time equivalent | same | same | $0 idle; $503.70 if continuously active |
| RunPod serverless RTX 4090 | $1.10 active | $0.0347 per 1,000 active-time equivalent | same | same | $0 idle; $803.00 if continuously active |
| Lium RTX 4090 observed offer | $0.35/GPU (offer was an 8-GPU node) | $0.1105 | $0.0368 | $0.0110 | $255.50/GPU; whole offered node $2,044 |
| Lium one-GPU A6000 observed offer | $0.42 | $0.1326 | $0.0442 | $0.0133 | $306.60 |

For scale-to-zero, load percentage does not create idle GPU cost; the meaningful number is active compute cost per 1,000. Cold start includes container start and roughly 8 GB of model weights unless cached, so expect tens of seconds to minutes and use asynchronous warm-up/retry behavior.

## djev versus DiffusionGemma

djev's weights have not been released; open-sourcing is announced, not available. It cannot be self-hosted today.

The djev-spark/OpenJev wrapper instead serves nvidia/diffusiongemma-26B-A4B-it-NVFP4. Hosting it would be **our DiffusionGemma service, not djev**. Current RunPod on-demand planning prices are H100 PCIe **$2.89/hour** (H100 SXM $3.49), RTX PRO 6000 96 GB **$2.09/hour**, and H200 **$4.59/hour**. Continuous 730-hour months are about **$2,110**, **$1,526**, and **$3,351** respectively, before storage and redundancy.

## What costs money now

- Sandy CPU container: existing PaaS capacity; no metered GPU.
- classifier.dev: $0 within its published free limits.
- GPU: **none running**. Lium and RunPod both list zero pods after the attempt.
- SemIf serverless: configured in code but not activated, hence $0.

Turning on SemIf needs a choice: RunPod 24 GB serverless ($0.69 active hour, scale to zero, cold starts) versus an always-on A5000 ($197/month) or another always-on venue. The default recommendation for this early gateway is serverless with min workers zero.

## Verification

- Local suite: 2 passed.
- Live-format upstream smoke test: classifier.dev answered the TypeSafe-shaped choice request correctly.
- Public repository contains the routing logic and provider states.
- No JevBench ranking changes or promotional edits were made. No X posts were made.
- Live HTTPS verification: /health returned 200, /models returned the expected states, the real classifier request returned red, and all five disclosure headers were present.
- Deployment revision: b22d2107efa197d5844083300aa82a893daac74c.

Sources checked 21 Sep 2026: TypeSafe MCA, classifier.dev terms/pricing/developers, SimpleJev demo page, djev public docs, RunPod pricing, and live authenticated Lium inventory.
