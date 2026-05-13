"""Type aliases for AI provider inputs and outputs.

Today the classifier and analyzer return loosely typed dicts to stay
compatible with the existing manifest. The aliases below document the
current shapes without enforcing them.

Classification dict (one per frame) — required keys:
    label: str
    conf: float
    key_text: str
    components: list[str]
    state: str

Analysis dict — keys produced by the current Gemini analyzer:
    domain_analysis, monetization_hypothesis, onboarding_hypothesis,
    feature_strategy_reasoning, copy_and_cta, product_bets,
    strategy_coherence, competitive_tier
"""

from typing import TypeAlias

Classification: TypeAlias = dict
Analysis: TypeAlias = dict
