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

Production basis: measured 21 September 2026 on the one Lium RTX PRO 6000 96 GB node now serving djev. Warm throughput was 14.372 decisions/s at concurrency 1, 34.684 at 4, and 58.422 at 16. Corresponding p50/p95 latencies were 0.069/0.072 s, 0.114/0.119 s, and 0.267/0.299 s. A stratified 20-item public image sample matched the H200 BF16 result item for item (11/20), so the cheaper card preserves the measured accuracy.

| Setup | Rate | Always-on day | Always-on month | $/1,000 at 10% | 30% | 100% |
|---|---:|---:|---:|---:|---:|---:|
| Lium RTX PRO 6000 96 GB, measured and live | $1.19/h | $28.56 | $868.70 | $0.0566 | $0.0189 | $0.00566 |
| Lium H100 80 GB, available but not selected | $1.30/h | $31.20 | $949.00 | — | — | — |
| Lium H200, earlier measured reference | $3.00/h | $72.00 | $2,190.00 | $0.0757 | $0.0252 | $0.00757 |

The live price is **$0.06 per 1,000 decisions**: the measured RTX PRO 6000 cost at 10% utilization is $0.0566, plus about a 6% infrastructure margin and rounded to a whole cent per 1,000. Actual early utilization may be lower; the public price is not silently changed per request.

The cheaper RTX 5090 NVFP4 path was attempted first. Lium's eight-card hosts did not permit a one-GPU split, and the only two-card split listing lacked the required ports and disappeared; no RTX 5090 instance was created. The A100 listing was $1.23/h per GPU ($29.52/day) but did not disclose 40 versus 80 GB, so it was not cheaper than the confirmed-fit RTX PRO 6000. The H100 crossed the user's approval boundary at $31.20/day. Estimated djev cold start is about two to four minutes with a cached checkpoint, longer on the first 49 GB download.

## Laya — 421M

The cheapest workable placement is Sandy CPU at effectively $0 incremental GPU cost. Existing measurements are p50 0.79s and p95 2.20s, but there is no concurrency throughput measurement, so a defensible $/1,000 estimate is not yet possible. External fallback rates begin at Lium RTX 3090 $0.16/h, RunPod A5000 $0.27/h, or RunPod serverless 24 GB $0.69 active-h.

## Decision

One Lium RTX PRO 6000 node runs djev continuously at $28.56/day, below the pre-approved $30/day ceiling. Docker restarts the model service automatically; Jev Router probes `/ready` every 15 seconds and removes djev from both meta routes while it is unhealthy.
