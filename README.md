# Jev Router

A neutral TypeSafe/Jev-compatible gateway for explicitly selected Jev-class providers.

There is no default model and no ranking-based routing. Callers choose model and may add an ordered fallback list. Responses identify the answering provider, latency, estimated cost, and zero markup in headers. Request content is not logged.

Example:

    curl https://jev-router.app.mintapis.com/v1/systemone -H 'content-type: application/json' -d '{"model":"classifier-fast","state":"Mia owns a red bicycle.","questions":{"color":{"type":"choice","instructions":"What color?","criteria":{"red":null,"blue":null}}}}'

See /models for live/disabled providers and the reason for each status. MIT licensed.

`djev` is wired to the open Apache-2.0 `Davipar/djev-dev` runtime. Set `DJEV_ENDPOINT` to its base URL (and optionally `DJEV_API_KEY`) to mark it live; with no endpoint it is deliberately listed as offline and costs nothing. This is an inference method over Google's Apache-2.0 DiffusionGemma checkpoint, not a separately trained djev model. The hosted Maisa API remains disabled because public proxy permission has not been established.
