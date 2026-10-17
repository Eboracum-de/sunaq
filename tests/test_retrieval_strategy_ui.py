from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "clients" / "nextcloud" / "sunaq"


def test_nextcloud_ui_exposes_safe_retrieval_strategy_inspector():
    template = (APP / "templates" / "main.php").read_text(encoding="utf-8")
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")
    css = (APP / "css" / "style.css").read_text(encoding="utf-8")

    assert 'id="sunaq-retrieval-strategy"' in template
    assert 'id="sunaq-retrieval-strategy-content"' in template
    assert "function renderRetrievalDecision(record)" in js
    assert "data.retrieval_decision" in js
    assert "retrievalStrategyContent.textContent" in js
    assert "retrievalStrategyContent.innerHTML" not in js
    assert "clearRetrievalDecision();" in js
    assert ".sunaq-retrieval-strategy" in css


def test_strategy_ui_names_the_three_audit_layers():
    js = (APP / "js" / "app.js").read_text(encoding="utf-8")

    assert "Analyse" in js
    assert "Planner" in js
    assert "Status" in js
    assert "Policy" in js
    assert "Effektiver Plan" in js
    assert "Entscheidungsgründe" in js
    assert "Resolver angefordert" in js
    assert "Resolver freigegeben" in js
    assert "Resolver effektiv" in js
    assert "Retriever" in js
