# Self-hosting estimates

Observed 21 September 2026. GPU inventory prices move; source rates are [Lium's live feed](https://lium.io/pricing.json), [Lium billing documentation](https://docs.lium.io/pod-users/billing), and [RunPod pricing](https://www.runpod.io/pricing). A month is 730 hours.

## SemIf — Qwen3.5-4B

The capacity basis is the earlier 8.8 decisions/s projection, not a fresh concurrency measurement.

| Setup | Rate | Always-on month | $/1,000 at 10% | 30% | 100% |
|---|---:|---:|---:|---:|---:|
| Lium RTX 3090 | $0.16/h | $116.80 | $0.0505 | $0.0168 | $0.0051 |
| RunPod A5000 pod | $0.27/h | $197.10 | $0.0852 | $0.0284 | $0.0085 |
| RunPod 24 GB serverless | $0.69 active-h | $503.70 if continuously active; $0 idle | $0.0218 active-compute | same | same |

Scale-to-zero estimate: 15–60 seconds cached, 1–5 minutes uncached. These model-specific figures are inferred from provider start ranges plus model load, not guarantees.

## djev — DiffusionGemma 26B

Capacity basis: measured 110.063 decisions/s at concurrency 16 on one H200.

| Setup | Rate | Always-on month | $/1,000 at 10% | 30% | 100% |
|---|---:|---:|---:|---:|---:|
| Lium H200 | $2.90/h | $2,117.00 | $0.0732 | $0.0244 | $0.0073 |
| RunPod H200 pod | $4.59/h | $3,350.70 | $0.1158 | $0.0386 | $0.0116 |
| RunPod H200 serverless | $5.93 active-h | $4,328.90 continuously; $0 idle | $0.0150 active-compute | same | same |

RunPod's RTX Pro 6000 fits the approximately 49.2 GiB runtime at $2.09/h ($1,525.70/month), but throughput is unmeasured, so no unit-cost claim is made. Estimated djev cold start is 30–120 seconds cached and potentially several minutes uncached.

## Laya — 421M

The cheapest workable placement is Sandy CPU at effectively $0 incremental GPU cost. Existing measurements are p50 0.79s and p95 2.20s, but there is no concurrency throughput measurement, so a defensible $/1,000 estimate is not yet possible. External fallback rates begin at Lium RTX 3090 $0.16/h, RunPod A5000 $0.27/h, or RunPod serverless 24 GB $0.69 active-h.

## Decision

Default to scale-to-zero/on-demand. No GPU is running. No always-on deployment above $150/month will be started without Florian's approval.
