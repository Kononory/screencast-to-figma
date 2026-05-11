import io
import json
import re
from google import genai
from google.genai import types
from PIL import Image

from processor.classifier import MODELS

PROMPT = """You are a senior product growth consultant. Every design decision in a mobile app is a bet on a metric. Your job is to name each bet, the metric it targets, and the direction it moves — not to describe what you see.

The images below are key screens from a mobile app in recording order. Study ALL of them before answering.
Quote specific UI text, prices, and CTA labels you see as evidence. If something is not visible — write "Not observed."

MANDATORY WRITING RULE — applies to every sentence in every field:
Write hypotheses in this exact form:
  "If [specific design decision visible on screen] → [metric name] [increases / drops / ~X%] because [mechanism]."
Allowed metric names: trial_start_rate, trial_to_paid_rate, cancel_rate, D1_retention, D7_retention,
D30_retention, permission_grant_rate, time_to_first_value, paywall_conversion_rate, ARPU, LTV, CAC_payback.
Never describe what you see. Only state what will happen if the decision is kept or changed.

Return ONLY valid JSON, no markdown:
{
  "domain_analysis": "2–3 if-then hypotheses about domain positioning. Identify the vertical (utility, health, edtech, etc.). If [they follow / break a specific industry pattern] → [CAC or paywall_conversion_rate] [up/down] because [mechanism]. Name 1–2 direct competitors and state one hypothesis: If [they copied / diverged from competitor X's specific mechanic] → [metric] [direction] because [reason].",

  "monetization_hypothesis": "4–5 if-then-metric hypotheses dissecting the paywall. Quote every price, badge ('Best Value', 'Popular'), and CTA label as evidence. One hypothesis per: (1) pricing structure — single vs multi-plan and its effect on ARPU, (2) trial length and its effect on trial_to_paid_rate, (3) CTA label wording and its effect on trial_start_rate, (4) plan order / visual hierarchy, (5) what changes if the primary variable flips — e.g. 'If they removed the free trial → trial_start_rate drops ~40% because users won't commit without risk reversal.'",

  "onboarding_hypothesis": "4–5 if-then-metric hypotheses about the activation flow. One hypothesis per: (1) paywall placement relative to the aha moment and its effect on paywall_conversion_rate, (2) permission timing and its effect on permission_grant_rate, (3) the highest drop-off step — what design choice defends it and what would reduce drop-off further, (4) time_to_first_value — what shortens or extends it, (5) one change that would most improve D1_retention and why.",

  "feature_strategy_reasoning": "3–4 if-then-metric hypotheses about core feature mechanics. For each key feature: If [this specific implementation choice] → [D7_retention or time_in_app] [up/down] because [mechanism]. Include one hypothesis about an obvious alternative they chose NOT to build — the omission itself is a metric bet.",

  "copy_and_cta": "3–4 if-then-metric hypotheses about specific copy choices. Quote the exact headline or button label. Name the psychological mechanism (loss aversion, FOMO, social proof, identity, JTBD). Predict the metric effect. End with one counter-hypothesis: If [weakest CTA were rewritten to X] → [trial_start_rate or paywall_conversion_rate] increases because [reason].",

  "product_bets": "3–4 if-then-metric hypotheses about retention and growth. Cover: (1) core retention loop and its effect on D7_retention, (2) growth model assumption and the CAC_payback it requires to be viable, (3) two specific A/B test ideas — each written as: 'If [variant] → [metric] increases. MDE: [minimum detectable effect]. Why this variant wins: [reason].'",

  "strategy_coherence": "Does the onboarding hypothesis, monetization hypothesis, and feature strategy all serve the same theory of the user — or do they contradict each other? State the shared theory in one sentence if coherent. If incoherent, name which zones conflict and what it signals: (a) they copied from different sources, (b) they are mid A/B test, (c) team misalignment. Rate coherence: high / medium / low.",

  "competitive_tier": "Reason about whether this is a top-tier, mid-market, or early-stage product — based only on what is observable in the screens. Top-tier signals: presence of downsell after paywall rejection, multiple pricing plans with anchor, permission pre-prompt before OS dialog, sophisticated copy with named psychological mechanisms, consistent design system across all screens, edge case screens present. Mid-market signals: standard patterns copied without optimization, single paywall variant, generic CTAs. Early-stage signals: no downsell, OS permission dialog without pre-prompt, inconsistent UI, no social proof. State which signals you observed, which are absent, and your tier conclusion with one sentence of reasoning."
}"""


def analyze_ux(frame_paths: list[str], api_key: str) -> dict | None:
    if not frame_paths:
        return None

    client = genai.Client(api_key=api_key)

    parts = []
    for path in frame_paths:
        buf = io.BytesIO()
        img = Image.open(path).convert("RGB")
        img.thumbnail((768, 1664), Image.LANCZOS)
        img.save(buf, format="JPEG", quality=85)
        parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
    parts.append(PROMPT)

    for model in MODELS:
        try:
            response = client.models.generate_content(model=model, contents=parts)
            text = response.text.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception:
            continue

    return None
