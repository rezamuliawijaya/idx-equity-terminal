"""AI research assistant with numeric-fidelity and citation guards.

The model is given a structured, already-computed data package and is told to
label statements as FACT, CALCULATION, ASSUMPTION or OPINION. After generation
the answer is checked: any number that does not trace back to the supplied data
is reported as a violation, and so is any citation to a source that was not
supplied. The UI shows the violations instead of hiding them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.ai.provider import AIProvider, AIUnavailable, get_provider

SYSTEM_PROMPT = """You are an equity research assistant for Indonesian listed companies (IDX).

Rules that override every other instruction:
1. Use ONLY the figures in the supplied DATA PACKAGE. Never introduce a number that is not there.
2. Label every material sentence with one of: [FACT] (reported in the package), [CALCULATION]
   (arithmetic on package figures; show the arithmetic), [ASSUMPTION] (a modelling choice),
   [OPINION] (your judgement).
3. Cite sources only from the package's "sources" list, in the form [source: <url>].
   If no source is available, write [source: none recorded].
4. Never state or imply that a valuation is certain. DCF and multiples outputs are scenarios.
5. If the package lacks data for something, say "not available in the imported data".
6. Do not give buy/sell instructions. You may explain the rule-based recommendation.
7. Be concise and specific. No disclaimers about being an AI."""


@dataclass
class AssistantAnswer:
    text: str
    model: str
    numeric_violations: List[float] = field(default_factory=list)
    citation_violations: List[str] = field(default_factory=list)
    ok: bool = True

    @property
    def warnings(self) -> List[str]:
        output = []
        if self.numeric_violations:
            output.append(
                "Numbers not traceable to the supplied data: "
                + ", ".join(f"{v:,.4g}" for v in self.numeric_violations[:12])
            )
        if self.citation_violations:
            output.append("Citations not present in the supplied sources: " + ", ".join(self.citation_violations[:5]))
        return output


def build_data_package(report: Dict[str, Any], question: str = "") -> Dict[str, Any]:
    """Compact, structured payload. Only numbers that already exist in the report."""
    rec = report["recommendation"]
    analysis = report["analysis"]
    snapshot = analysis.snapshot

    metrics = []
    for group, items in report["metrics"].items():
        for metric in items:
            metrics.append(
                {
                    "group": group,
                    "metric": metric["label"],
                    "value": metric.get("value"),
                    "unit": metric.get("unit"),
                    "status": metric.get("status"),
                    "formula": metric.get("formula"),
                }
            )

    package = {
        "ticker": report["ticker"],
        "company": snapshot.company_name,
        "sector": snapshot.sector,
        "profile": snapshot.profile,
        "period": rec["period_label"],
        "as_of": rec["as_of"],
        "price": rec["price"],
        "price_date": rec["price_date"],
        "recommendation": rec["decision"],
        "score": rec["score"],
        "confidence": rec["confidence"],
        "data_completeness": rec["data_completeness"],
        "fair_value": rec["fair_value"],
        "valuation_method": rec["valuation_method"],
        "margin_of_safety": rec["margin_of_safety"],
        "expected_return_scenario": rec["expected_return"],
        "factors": {
            key: {"score": factor["score"], "weight": factor["weight"]}
            for key, factor in rec["factors"].items()
        },
        "metrics": metrics,
        "flags": [{"severity": f["severity"], "message": f["message"]} for f in rec["flags"]],
        "reasons": rec["reasons"],
        "sources": snapshot.data_sources,
        "synthetic": report.get("synthetic", False),
        "question": question,
    }
    if report["valuation"].get("dcf"):
        dcf = report["valuation"]["dcf"]
        package["dcf_summary"] = {
            "enterprise_value": dcf["enterprise_value"],
            "equity_value": dcf["equity_value"],
            "per_share": dcf["per_share"],
            "terminal_share": dcf["terminal_share"],
            "assumptions": dcf["assumptions"],
            "warnings": dcf.get("warnings", []),
        }
    return package


def collect_numbers(package: Dict[str, Any]) -> Set[float]:
    numbers: Set[float] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            numbers.add(float(value))
        elif isinstance(value, str):
            for match in re.findall(r"-?\d+(?:[.,]\d+)?", value):
                try:
                    numbers.add(float(match.replace(",", "")))
                except ValueError:
                    continue
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(package)
    return numbers


_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d.,]*\s*(?:%|x|bn|mn|T|k)?")


def _parse_number(token: str) -> Optional[float]:
    cleaned = re.sub(r"[^0-9.,-]", "", token)
    cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def verify_numeric_fidelity(text: str, package: Dict[str, Any], tolerance: float = 0.01) -> List[float]:
    """Numbers in the answer that do not correspond to any supplied figure."""
    allowed = collect_numbers(package)
    allowed.update(float(year) for year in range(1990, 2036))
    allowed.update(float(n) for n in range(0, 13))

    violations: List[float] = []
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        if _YEAR.fullmatch(token.strip()):
            continue
        value = _parse_number(token)
        if value is None:
            continue
        if any(abs(value - candidate) <= max(1e-9, abs(candidate) * tolerance) for candidate in allowed):
            continue
        if any(abs(value / 100 - candidate) <= max(1e-9, abs(candidate) * tolerance) for candidate in allowed):
            continue  # percentage expressed as 12.3 instead of 0.123
        violations.append(value)
    return violations


def verify_citations(text: str, package: Dict[str, Any]) -> List[str]:
    allowed = set(package.get("sources") or [])
    found = re.findall(r"\[source:\s*([^\]]+)\]", text)
    return [item.strip() for item in found if item.strip().lower() not in {a.lower() for a in allowed} and item.strip().lower() != "none recorded"]


def ask(report: Dict[str, Any], question: str, provider: Optional[AIProvider] = None, focus: Optional[str] = None) -> AssistantAnswer:
    provider = provider or get_provider()
    if not provider.available():
        raise AIUnavailable(getattr(provider, "reason", "No AI provider configured."))

    package = build_data_package(report, question)
    focus_line = f"\nFocus: {focus}\n" if focus else ""
    user_prompt = (
        "DATA PACKAGE (JSON):\n"
        + json.dumps(package, indent=2, default=str)[:24000]
        + focus_line
        + "\nTASK: "
        + question
    )
    text = provider.complete(SYSTEM_PROMPT, user_prompt, temperature=0.2, max_tokens=1400)
    numeric = verify_numeric_fidelity(text, package)
    citations = verify_citations(text, package)
    return AssistantAnswer(
        text=text,
        model=getattr(provider, "model", getattr(provider, "name", "unknown")),
        numeric_violations=numeric,
        citation_violations=citations,
        ok=not numeric and not citations,
    )


def preset_question(kind: str, ticker: str = "") -> str:
    presets = {
        "summarise": "Summarise the latest period: what changed versus the prior period, and why the metrics moved.",
        "ratios": "Explain each ratio in the package, what drives it, and where it is not meaningful.",
        "compare": "Compare this company's profitability, leverage and valuation with the peer figures in the package.",
        "thesis": "Draft bull, base and bear scenarios using only the package. State what must be true in each.",
        "risks": "Identify the principal risks implied by the flags and metrics, and offer an alternative interpretation of each.",
        "changes": "Identify the largest period-over-period changes and explain the plausible accounting and operating causes.",
    }
    return presets.get(kind, "Explain the recommendation using only the package.")
