import os
from pathlib import Path

# The test suite uses a fixed fixture catalogue and never probes real providers.
os.environ.setdefault("CATALOGUE_PATH", str(Path(__file__).with_name("catalogue.test.json")))
os.environ.setdefault("PROBE_PROVIDERS", "false")
for name in ("SEMIF_ENDPOINT", "DJEV_ENDPOINT", "DECIDER_ENDPOINT", "BYOK_DEMO_ENDPOINT"):
    os.environ.setdefault(name, "http://127.0.0.1:9")
