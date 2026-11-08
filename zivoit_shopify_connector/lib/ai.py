"""Bring-your-own-key LLM gateway plus the local engines that need no key.

Every AI feature of the connector has a deterministic local path. The LLM, when
configured, only refines that result, so the connector keeps working when no
key is set or when the provider is down.
"""
import difflib
import json
import logging
import re

import requests

_logger = logging.getLogger(__name__)

TIMEOUT = 60

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5-5",
    "gemini": "gemini-2.5-flash",
    "compatible": "",
}


class AIError(Exception):
    pass


def complete(provider, api_key, model, system, prompt, base_url=None, max_tokens=900, temperature=0.2):
    """Return ``(text, usage)``, where usage holds input and output tokens."""
    model = model or DEFAULT_MODELS.get(provider) or ""
    if not model:
        raise AIError("No AI model is configured.")
    try:
        if provider == "anthropic":
            return _anthropic(api_key, model, system, prompt, base_url, max_tokens, temperature)
        if provider == "gemini":
            return _gemini(api_key, model, system, prompt, base_url, max_tokens, temperature)
        return _openai(api_key, model, system, prompt, base_url, max_tokens, temperature)
    except requests.RequestException as error:
        raise AIError(f"AI provider unreachable: {error}") from error


def _check(response):
    if response.status_code >= 400:
        raise AIError(f"AI provider answered HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def _openai(api_key, model, system, prompt, base_url, max_tokens, temperature):
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = _check(requests.post(url, headers=headers, timeout=TIMEOUT, json={
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }))
    usage = data.get("usage") or {}
    return data["choices"][0]["message"]["content"], {
        "input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0),
    }


def _anthropic(api_key, model, system, prompt, base_url, max_tokens, temperature):
    url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    data = _check(requests.post(url, timeout=TIMEOUT, headers={
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json={
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }))
    usage = data.get("usage") or {}
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    return text, {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)}


def _gemini(api_key, model, system, prompt, base_url, max_tokens, temperature):
    url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    data = _check(requests.post(
        f"{url}/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key or "", "Content-Type": "application/json"},
        timeout=TIMEOUT,
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        },
    ))
    usage = data.get("usageMetadata") or {}
    candidates = data.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts), {
        "input": usage.get("promptTokenCount", 0), "output": usage.get("candidatesTokenCount", 0),
    }


def extract_json(text):
    """Parse the first JSON object or array found in a model answer."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except ValueError:
                return None
    return None


# ----------------------------------------------------------------------
# Local product matching
# ----------------------------------------------------------------------
STOP_WORDS = {"the", "a", "an", "and", "for", "with", "of", "default", "title", "new"}


def normalize_code(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _tokens(value):
    return {t for t in re.findall(r"[a-z0-9]+", (value or "").lower()) if t not in STOP_WORDS}


def similarity(left, right):
    """Blend of edit similarity and token overlap, between 0 and 1."""
    if not left or not right:
        return 0.0
    ratio = difflib.SequenceMatcher(None, left.lower(), right.lower()).ratio()
    a, b = _tokens(left), _tokens(right)
    overlap = len(a & b) / len(a | b) if a and b else 0.0
    return round(0.5 * ratio + 0.5 * overlap, 4)


def score_product_match(shop_sku, shop_barcode, shop_title, odoo_sku, odoo_barcode, odoo_name):
    """Return ``(confidence, reason)``, the score of one candidate."""
    if shop_barcode and odoo_barcode and shop_barcode == odoo_barcode:
        return 1.0, "Identical barcode"
    if shop_sku and odoo_sku and shop_sku == odoo_sku:
        return 1.0, "Identical SKU"
    left, right = normalize_code(shop_sku), normalize_code(odoo_sku)
    if left and left == right:
        return 0.97, "Same SKU after normalising punctuation and case"
    if left and right and (left in right or right in left) and min(len(left), len(right)) >= 5:
        title_score = similarity(shop_title, odoo_name)
        return round(min(0.75 + title_score * 0.2, 0.95), 4), "SKU contained in the other, similar title"
    score = similarity(shop_title, odoo_name)
    return round(score * 0.9, 4), f"Title similarity {round(score * 100)}%"


# ----------------------------------------------------------------------
# Local order risk
# ----------------------------------------------------------------------
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
}
SHOPIFY_LEVEL_POINTS = {"HIGH": 55, "MEDIUM": 25, "LOW": 0, "NONE": 0, "PENDING": 0}
SHOPIFY_RECOMMENDATION_POINTS = {"CANCEL": 35, "INVESTIGATE": 20, "ACCEPT": 0, "NONE": 0}


def score_order_risk(signals):
    """Explainable additive score, 0 to 100. Returns ``(score, reasons)``."""
    points = 0
    reasons = []

    def add(value, reason):
        nonlocal points
        points += value
        reasons.append(f"+{value} {reason}")

    level = signals.get("shopify_level")
    if SHOPIFY_LEVEL_POINTS.get(level):
        add(SHOPIFY_LEVEL_POINTS[level], f"Shopify risk level is {level}")
    recommendation = signals.get("shopify_recommendation")
    if SHOPIFY_RECOMMENDATION_POINTS.get(recommendation):
        add(SHOPIFY_RECOMMENDATION_POINTS[recommendation], f"Shopify recommends {recommendation}")
    if signals.get("address_mismatch"):
        add(12, "Billing and shipping addresses differ")
    if signals.get("country_mismatch"):
        add(10, "Billing and shipping countries differ")
    if signals.get("disposable_email"):
        add(20, "Disposable email domain")
    if signals.get("no_contact"):
        add(10, "No email or phone on the order")
    orders = signals.get("customer_orders", 0)
    ratio = signals.get("value_ratio") or 0
    if orders <= 1 and ratio >= 3:
        add(15, f"First order at {ratio:.1f}x the store average value")
    elif ratio >= 5:
        add(10, f"Order at {ratio:.1f}x the store average value")
    if signals.get("recent_orders_same_contact", 0) >= 3:
        add(15, "3 or more orders from this contact in the last 24 hours")
    if signals.get("failed_payments", 0) >= 2:
        add(12, "Several failed payment attempts")
    if not reasons:
        reasons.append("No risk signal found")
    return min(points, 100), reasons


def is_disposable_email(email):
    return (email or "").rsplit("@", 1)[-1].lower() in DISPOSABLE_DOMAINS


# ----------------------------------------------------------------------
# Local error diagnosis
# ----------------------------------------------------------------------
ERROR_RULES = [
    (r"access.?denied|403|required access|scope", "Missing Shopify access scope",
     "Open the app in the Shopify Dev Dashboard, add the scope named in the error, release a new "
     "version, and re-test the connection. Customer and order fields also need protected customer "
     "data approval."),
    (r"401|unauthori[sz]ed|invalid api key|access token", "Credentials rejected",
     "Re-enter the Client ID and Client Secret on the store, then press Test Connection."),
    (r"402|frozen|payment required", "Shopify store frozen",
     "The Shopify plan has a billing problem. Settle it in Shopify; jobs can then be retried."),
    (r"unmapped variant|no odoo product|product not found", "Shopify variant is not linked to an Odoo product",
     "Open AI Product Matches, accept or set the product, then retry. Or enable Auto-create Products."),
    (r"not stocked|inventory item.*location|not.*activated", "Item not stocked at this Shopify location",
     "Enable the location for this item in Shopify, or re-run the export so the connector activates it."),
    (r"throttl|429|budget", "Shopify rate limit",
     "Temporary. The queue retries automatically with a delay."),
    (r"timeout|timed out|connection|network|50[0-9]", "Network or Shopify outage",
     "Temporary. The queue retries automatically with a delay."),
    (r"no.*fulfillment order|fulfillment order|not.*fulfillable|already fulfilled", "Nothing left to fulfil on Shopify",
     "The order was probably fulfilled or changed in Shopify. Re-import the order to refresh its state."),
    (r"lock date|locked|fiscal", "Accounting period locked",
     "Change the accounting lock date or post the document manually, then retry."),
    (r"journal|account.*(not|missing|required)|outstanding", "Accounting configuration incomplete",
     "Review the workflow journal and the payout accounts on the store settings."),
    (r"tax", "Tax could not be mapped",
     "Enable Auto-create Taxes on the store, or create a sales tax with the same rate."),
    (r"serial|lot", "Tracked product needs a lot or serial number",
     "Validate this delivery manually in Odoo so the lot or serial can be chosen."),
    (r"handle|already exists|has already been taken|duplicate", "Duplicate on Shopify",
     "Link the existing Shopify record through a product import instead of creating a new one."),
]


def diagnose_error(message):
    """Return ``(root_cause, fix, transient_hint)`` from the local rule set."""
    text = (message or "").lower()
    for pattern, cause, fix in ERROR_RULES:
        if re.search(pattern, text):
            transient = cause in ("Shopify rate limit", "Network or Shopify outage")
            return cause, fix, transient
    return "Unclassified error", "Read the technical details below. Retry once the cause is fixed.", False
