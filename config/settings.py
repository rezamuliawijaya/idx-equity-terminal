"""Central configuration for the Indonesian Equity Research & Decision Terminal.

Every threshold in this module is an *initial research parameter*, not a proven
optimal value. Thresholds can be overridden with environment variables or with
the Settings page, which persists overrides in the local SQLite database.

Nothing here contacts a network or imports a third-party package, so the module
stays importable from tests and from CLI scripts.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("TERMINAL_DATA_DIR", BASE_DIR / "data"))
IMPORT_DIR = DATA_DIR / "imports"
SAMPLE_DIR = DATA_DIR / "sample"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
DOCUMENT_DIR = DATA_DIR / "documents"

DB_PATH = Path(os.getenv("TERMINAL_DB_PATH", DATA_DIR / "terminal.db"))

APP_NAME = "Indonesian Equity Research & Decision Terminal"
APP_VERSION = "0.1.0"

# --------------------------------------------------------------------------- #
# Sector mapping
# --------------------------------------------------------------------------- #

# Sector names follow the IDX-IC style taxonomy closely enough to be useful while
# remaining free text. ``resolve_profile`` also accepts looser labels.
SECTOR_PROFILES: Dict[str, str] = {
    "Financials": "bank",
    "Basic Materials": "mining",
    "Energy": "mining",
    "Industrials": "manufacturing",
    "Consumer Non-Cyclicals": "consumer",
    "Consumer Cyclicals": "consumer",
    "Healthcare": "consumer",
    "Infrastructure": "property",
    "Properties & Real Estate": "property",
    "Technology": "generic",
    "Transportation & Logistics": "generic",
    "Unclassified": "generic",
}

PROFILE_ALIASES = {
    "bank": "bank",
    "banks": "bank",
    "financial": "bank",
    "financials": "bank",
    "financial institution": "bank",
    "insurance": "bank",
    "manufactur": "manufacturing",
    "industrial": "manufacturing",
    "industrials": "manufacturing",
    "basic industri": "manufacturing",
    "consumer": "consumer",
    "retail": "consumer",
    "healthcare": "consumer",
    "mining": "mining",
    "commodity": "mining",
    "energy": "mining",
    "property": "property",
    "real estate": "property",
    "infrastructure": "property",
    "construction": "property",
}


def resolve_profile(sector: Optional[str]) -> str:
    """Map a free-text sector label to a scoring profile key."""
    if not sector:
        return "generic"
    text = str(sector).strip().lower()
    if text in {k.lower(): v for k, v in SECTOR_PROFILES.items()}:
        for key, value in SECTOR_PROFILES.items():
            if key.lower() == text:
                return value
    for needle, profile in PROFILE_ALIASES.items():
        if needle in text:
            return profile
    return "generic"


# --------------------------------------------------------------------------- #
# Sectors that have a meaningful EV/EBITDA multiple
# --------------------------------------------------------------------------- #

EV_MULTIPLE_PROFILES = {"manufacturing", "consumer", "mining", "property", "generic"}


# --------------------------------------------------------------------------- #
# Configuration objects
# --------------------------------------------------------------------------- #


@dataclass
class ScoringWeights:
    """Factor weights for the composite fundamental score (must sum to 1.0)."""

    quality: float = 0.30
    growth: float = 0.20
    valuation: float = 0.30
    health: float = 0.10
    momentum: float = 0.10

    def normalized(self) -> "ScoringWeights":
        total = self.quality + self.growth + self.valuation + self.health + self.momentum
        if total <= 0:
            raise ValueError("Scoring weights must sum to a positive value")
        return ScoringWeights(
            quality=self.quality / total,
            growth=self.growth / total,
            valuation=self.valuation / total,
            health=self.health / total,
            momentum=self.momentum / total,
        )

    def as_dict(self) -> Dict[str, float]:
        return asdict(self.normalized())


@dataclass
class DecisionPolicy:
    """Rules that translate scores into a recommendation.

    Thresholds are deliberately conservative and fully configurable. They are
    research defaults, not universal investment rules.
    """

    buy_score: float = 65.0
    buy_min_margin_of_safety: float = 0.15
    buy_min_confidence: float = 0.70
    buy_require_positive_earnings: bool = True

    hold_score: float = 45.0
    sell_score: float = 30.0

    # A confidence below this value blocks any actionable recommendation.
    min_confidence: float = 0.55
    # Coverage of factor weights required to score at all.
    min_factor_coverage: float = 0.45

    max_fundamental_age_days: int = 550
    max_price_age_days: int = 30

    # Margin of safety required for the expected-return scenario to be labelled
    # attractive at all.
    attractive_margin_of_safety: float = 0.10


@dataclass
class TradingCosts:
    """Configurable IDX trading assumptions.

    Defaults reflect commonly cited retail brokerage rates and the 0.1% sales
    tax. Verify against your own broker before relying on them.
    """

    lot_size: int = 100
    buy_fee_rate: float = 0.0015
    sell_fee_rate: float = 0.0025  # includes 0.1% sales tax by default
    min_fee: float = 0.0
    slippage_rate: float = 0.001
    max_position_pct: float = 0.25
    max_sector_pct: float = 0.45


@dataclass
class DCFDefaults:
    projection_years: int = 5
    terminal_growth: float = 0.025
    wacc: float = 0.105
    tax_rate: float = 0.22
    revenue_growth: float = 0.08
    operating_margin: Optional[float] = None  # None -> use historical
    capex_pct_revenue: Optional[float] = None
    da_pct_revenue: Optional[float] = None
    nwc_pct_revenue_change: float = 0.10
    fade_growth_to_terminal: bool = True


@dataclass
class TerminalSettings:
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    policy: DecisionPolicy = field(default_factory=DecisionPolicy)
    costs: TradingCosts = field(default_factory=TradingCosts)
    dcf: DCFDefaults = field(default_factory=DCFDefaults)
    display_currency: str = "IDR"
    # Blended fair value weights for the methods that are available.
    dcf_weight: float = 0.6
    relative_weight: float = 0.4
    theme: str = "dark"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TerminalSettings":
        base = cls()
        if not payload:
            return base
        return cls(
            weights=_merge_dataclass(base.weights, payload.get("weights")),
            policy=_merge_dataclass(base.policy, payload.get("policy")),
            costs=_merge_dataclass(base.costs, payload.get("costs")),
            dcf=_merge_dataclass(base.dcf, payload.get("dcf")),
            display_currency=payload.get("display_currency", base.display_currency),
            dcf_weight=float(payload.get("dcf_weight", base.dcf_weight)),
            relative_weight=float(payload.get("relative_weight", base.relative_weight)),
            theme=payload.get("theme", base.theme),
        )


def _merge_dataclass(instance: Any, overrides: Optional[Dict[str, Any]]) -> Any:
    """Copy ``instance`` and apply only keys that exist on the dataclass."""
    if not overrides:
        return instance
    allowed = {f for f in instance.__dataclass_fields__}  # type: ignore[attr-defined]
    clean = {k: v for k, v in overrides.items() if k in allowed}
    return replace(instance, **clean)


SETTINGS = TerminalSettings()


# --------------------------------------------------------------------------- #
# Environment handling (minimal .env loader, no extra dependency)
# --------------------------------------------------------------------------- #


def load_dotenv(path: Optional[Path] = None) -> int:
    """Load ``.env`` into ``os.environ`` without overwriting existing values.

    Returns the number of variables set. Never raises on a malformed file.
    """
    env_path = Path(path) if path else BASE_DIR / ".env"
    if not env_path.exists():
        return 0
    count = 0
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def ensure_directories() -> None:
    for directory in (DATA_DIR, IMPORT_DIR, SAMPLE_DIR, EXPORT_DIR, BACKUP_DIR, DOCUMENT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


load_dotenv()
ensure_directories()
