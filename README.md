# Jev Router

A neutral TypeSafe/Jev-compatible gateway for explicitly selected Jev-class providers.

There is no default model and no ranking-based routing. Callers choose model and may add an ordered fallback list. Responses identify the answering provider, latency, estimated cost, and zero markup in headers. Request content is not logged.

Example:

    curl https://jev-router.app.mintapis.com/v1/systemone -H 'content-type: application/json' -d '{"model":"classifier-fast","state":"Mia owns a red bicycle.","questions":{"color":{"type":"choice","instructions":"What color?","criteria":{"red":null,"blue":null}}}}'

See /models for live/disabled providers and the reason for each status. MIT licensed.
