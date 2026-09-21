# Jev Router

Jev Router is neutral infrastructure for explicitly selected Jev-class decision models. It preserves the TypeSafe System One request and answer shape, supports caller-ordered fallbacks, and never selects a model from a benchmark score.

Public routes:

- `POST /v1/systemone` — text decisions
- `POST /v1/multimodal` — image decisions using bounded base64 data URLs
- `POST /v1/chat/completions` — small non-streaming convenience adapter
- `GET /models` — availability, modalities, prices, and separately labeled benchmark data
- `/docs`, `/status`, `/terms`, `/privacy`, `/refunds`, `/impressum`

Accounts use Google sign-in. API keys are shown once, stored as hashes, and revocable. Self-hosted usage draws from a prepaid micro-USD ledger credited only by signature-verified, idempotent Stripe webhooks. Third-party routes never use operator keys for customer traffic.

## Run locally

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=/tmp/jev-router.db SESSION_SECRET=local-only uvicorn app:app --reload
```

Production configuration uses `APP_URL`, `SESSION_SECRET`, Google OAuth variables, Stripe test variables, and optional self-hosted endpoint variables. Request bodies and images are neither logged nor stored.

Provider-rights evidence is in [PROVIDER-RIGHTS.md](PROVIDER-RIGHTS.md); infrastructure estimates are in [COSTS.md](COSTS.md).
