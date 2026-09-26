from pathlib import Path

from routing import Health, load_catalogue, rank

REAL = Path(__file__).resolve().parents[1] / "catalogue.json"


def test_real_catalogue_paid_routes_are_own_open_models():
    cat = load_catalogue(REAL)
    paid = {k: v for k, v in cat.items() if v["routing"] == "paid"}
    assert set(paid) == {"jeff-gliformer-400m", "verdict-1.4-151m", "gliner2-large"}
    for entry in paid.values():
        assert entry["terms_basis"] and entry["licence"]
        assert "Apache-2.0" in entry["licence"] or "MIT" in entry["licence"]
        assert entry["price_per_1k_cents"] >= 1
    assert cat["typesafe-jev"]["routing"] == "listed"


def test_auto_without_provider_keys_uses_best_paid_model_then_fails_over():
    cat = load_catalogue(REAL)
    order = rank(cat, Health(), image=False, question_types={"choice"})
    assert order[:3] == ["jeff-gliformer-400m", "verdict-1.4-151m", "gliner2-large"]
    assert rank(cat, Health(), image=False, question_types={"choice"}, prefer="price")[0] == "verdict-1.4-151m"
