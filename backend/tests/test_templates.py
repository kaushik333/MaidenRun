from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"

CONTRACT_TERMS = ["docker-compose.yml", "Dockerfile", "8001", "8002", "8003"]


def test_compatibility_template_covers_the_contract() -> None:
    text = (TEMPLATES_DIR / "compatibility.md").read_text()
    assert text.strip()
    for term in CONTRACT_TERMS:
        assert term in text


def test_adapt_template_covers_the_contract_and_trust_boundary() -> None:
    text = (TEMPLATES_DIR / "adapt.md").read_text()
    assert text.strip()
    for term in CONTRACT_TERMS:
        assert term in text
    assert "never runs this" in text
    assert "Review" in text or "review" in text
