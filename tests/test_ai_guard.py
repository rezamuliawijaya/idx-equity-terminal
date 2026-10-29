"""The AI assistant is optional and guarded.

No network call is made here: the provider is never enabled in tests, and the
guards are pure functions over the answer text.
"""

from __future__ import annotations

import pytest

from src.ai.provider import AIUnavailable, NullProvider, provider_config, redact
from src.ai.research_assistant import (
    collect_numbers,
    verify_citations,
    verify_numeric_fidelity,
)

PACKAGE = {
    "ticker": "TEST",
    "price": 1000.0,
    "fair_value": 1234.5,
    "margin_of_safety": 0.19,
    "score": 72.5,
    "confidence": 0.81,
    "sources": ["https://example.test/filing-2023"],
    "flags": [{"severity": "medium", "message": "Customer concentration"}],
    "metrics": [{"metric": "ROE", "value": 0.183}],
    "question": "Is the valuation supported?",
}


def test_numbers_in_the_package_are_accepted():
    text = "Fair value is 1,234.5 at a price of 1000, a 19% margin of safety. Score 72.5."
    assert verify_numeric_fidelity(text, PACKAGE) == []


def test_numbers_outside_the_package_are_flagged():
    text = "The share trades at 1000 and the hidden catalyst is worth 987.65 per share."
    violations = verify_numeric_fidelity(text, PACKAGE)
    assert violations == [pytest.approx(987.65)]


def test_percentages_may_be_expressed_as_points():
    text = "Margin of safety is 19.0%."
    assert verify_numeric_fidelity(text, PACKAGE) == []


def test_years_are_not_treated_as_fabricated_figures():
    assert verify_numeric_fidelity("In 2024 the company reported results.", PACKAGE) == []


def test_citations_must_come_from_the_package():
    assert verify_citations("Revenue grew [source: https://example.test/filing-2023].", PACKAGE) == []
    assert verify_citations("Revenue grew [source: none recorded].", PACKAGE) == []
    assert verify_citations("Revenue grew [source: https://invented.example/x].", PACKAGE) == [
        "https://invented.example/x"
    ]


def test_collect_numbers_walks_nested_structures_and_strings():
    numbers = collect_numbers({"a": 1.0, "b": ["2.5", {"c": None, "d": True}], "e": "score 72.5"})
    assert {1.0, 2.5, 72.5}.issubset(numbers)
    # Booleans are labels, not figures.
    assert collect_numbers({"flag": True, "other": False}) == set()


def test_null_provider_fails_loudly_instead_of_inventing_an_answer():
    provider = NullProvider(reason="No AI provider configured.")
    assert provider.available() is False
    with pytest.raises(AIUnavailable, match="No AI provider configured"):
        provider.complete("system", "user")


def test_api_keys_are_redacted_in_status_output(monkeypatch):
    assert redact("") == "(not set)"
    assert redact("short") == "(set)"
    assert redact("sk-1234567890abcdef") == "sk-1...cdef"

    monkeypatch.setenv("AI_API_KEY", "sk-1234567890abcdef")
    config = provider_config()
    assert config["configured"] is True
    assert "1234567890" not in str(config)
