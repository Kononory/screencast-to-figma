import json
import re
from google import genai

from processor.classifier import MODELS

PROMPT = """You are a senior product strategist comparing two mobile apps from the same category.

Below are two strategic profiles. Each contains zone-level hypotheses (onboarding, monetization, feature) and a coherence assessment.

Your job is NOT to list differences — it is to reason about what each difference reveals about the strategic bet behind it.

Return ONLY valid JSON, no markdown:
{
  "user_theory_diff": "In one sentence per app: what theory of their user does each app operate on? Then: are these theories fundamentally different, or variations of the same bet? What does the divergence (or convergence) reveal about how each team reads the market?",

  "onboarding_strategy_diff": "Compare onboarding hypotheses. Do not list screen counts — reason about what each activation approach assumes about user motivation, prior awareness, and willingness to invest time. Which approach is riskier and why? Which has higher upside if correct?",

  "monetization_strategy_diff": "Compare paywall hypotheses. Reason about what pricing structure, trial logic, and CTA choices reveal about each team's understanding of price sensitivity and user trust. Which monetization strategy requires a stronger top-of-funnel to be viable?",

  "feature_strategy_diff": "Compare feature hypotheses. What does each app believe is the core retention mechanism? If both target the same user need, who has the stronger answer — and what would it take for the weaker one to catch up?",

  "coherence_diff": "Compare strategic coherence scores. If one app is more coherent: what does that signal about team alignment, data maturity, or A/B testing sophistication? If both are incoherent in different ways: what does each incoherence pattern reveal?",

  "competitive_verdict": "Which app is playing a stronger long-term game and why — not based on surface patterns, but based on the internal logic of their strategy. What is the one thing the weaker app could change that would most close the gap? What is the one thing the stronger app is betting on that could backfire?"
}

APP A PROFILE:
{profile_a}

APP B PROFILE:
{profile_b}"""


def compare_sessions(profile_a: dict, profile_b: dict, api_key: str) -> dict | None:
    client = genai.Client(api_key=api_key)

    prompt = PROMPT.format(
        profile_a=json.dumps(profile_a, indent=2),
        profile_b=json.dumps(profile_b, indent=2),
    )

    for model in MODELS:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            text = response.text.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception:
            continue

    return None
