from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"

CONTRACT_TERMS = ["docker-compose.yml", "Dockerfile", "8001", "8002", "8003"]


def test_compatibility_template_covers_the_contract() -> None:
    text = (TEMPLATES_DIR / "compatibility.md").read_text()
    assert text.strip()
    for term in CONTRACT_TERMS:
        assert term in text
