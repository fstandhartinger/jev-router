# Jev Router

Jev Router hosts open decision models (Jev-class) on demand. A signed-in user starts a dedicated GPU—or lets the first request start it—sees the published cold-start estimate and a live hosted-time meter, and pays per started minute from prepaid credit. Idle instances turn cold automatically.

Warm shared models can also be used per decision through concrete routes or two transparent meta names:

- `jev-class`: healthy text models in descending JevBench score order.
- `image-jev-class`: healthy image models in descending image-benchmark order.

## Hosting safeguards

- Provider cost plus a disclosed 10% infrastructure margin, rounded to micro-USD per minute.
- First minute reserved before provisioning; no postpaid balance.
- Automatic stop when prepaid credit cannot cover the next minute.
- Adjustable idle shutdown from 2–60 minutes (default 10).
- Per-account and global concurrent-instance caps.
- Daily global provider-spend ceiling.
- A reaper runs every two minutes, lists provider instances, deletes tagged orphans, and marks missing instances stopped.
- Lium is the primary provider and RunPod is the fallback behind the same control interface.

## API

- `POST /v1/systemone` and `POST /v1/multimodal` — shared per-decision routes
- `GET /models` — shared route availability and prices
- `GET /hosting/models` — hosted prices, real provider cost, margin, and cold-start estimate
- `POST /hosting/start` — start one dedicated instance (signed-in browser session)
- `GET /hosting/instances` — live hosted seconds, billed minutes, and meter total
- `POST /hosting/{id}/stop` — stop and verify provider deletion

Accounts use Google sign-in. Stripe remains in test mode. API keys are shown once and stored as hashes; request bodies and images are not stored.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_PATH=/tmp/jev-router.db SESSION_SECRET=local-only .venv/bin/uvicorn app:app --reload
```

`HOSTING_CONTROL_URL` and `HOSTING_CONTROL_TOKEN` point to the narrow provider controller. Without both, start requests fail closed while the catalogue and shared routes remain available.
