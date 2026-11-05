import json
import logging
import re
import time

import requests

from odoo import api, models

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "zivoit_epc_ai."
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5-5",
    "openai_compatible": "",
}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "openai_compatible": "",
}
MAX_LOG_CHARS = 20000


class EpcAiError(Exception):
    """Raised when the configured LLM provider cannot answer."""


class EpcAiService(models.AbstractModel):
    """Provider-agnostic LLM gateway with deterministic fallback.

    Every feature calls :meth:`ask`. If no provider is configured, or the call
    fails, or the answer cannot be parsed, the feature's own rule-based fallback
    is used instead, so the module keeps working offline. Every call is logged
    in ``epc.ai.log`` for audit.
    """

    _name = "epc.ai.service"
    _description = "ZivoIT EPC AI Service"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env["ir.config_parameter"].sudo()
        provider = icp.get_param(PARAM_PREFIX + "provider", "none") or "none"
        timeout = icp.get_param(PARAM_PREFIX + "timeout", "60") or "60"
        try:
            timeout = max(5, int(timeout))
        except ValueError:
            timeout = 60
        return {
            "provider": provider,
            "api_key": icp.get_param(PARAM_PREFIX + "api_key", "") or "",
            "model": icp.get_param(PARAM_PREFIX + "model", "") or DEFAULT_MODELS.get(provider, ""),
            "base_url": (icp.get_param(PARAM_PREFIX + "base_url", "") or DEFAULT_BASE_URLS.get(provider, "")).rstrip("/"),
            "timeout": timeout,
        }

    @api.model
    def is_enabled(self):
        config = self._get_config()
        if config["provider"] == "none":
            return False
        if config["provider"] in ("openai", "anthropic") and not config["api_key"]:
            return False
        if config["provider"] == "openai_compatible" and not config["base_url"]:
            return False
        return bool(config["model"])

    # ------------------------------------------------------------------
    # Low level calls
    # ------------------------------------------------------------------
    @api.model
    def _call_openai(self, config, system, prompt, max_tokens, images=None):
        headers = {"Content-Type": "application/json"}
        if config["api_key"]:
            headers["Authorization"] = "Bearer %s" % config["api_key"]
        payload = {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": self._openai_content(prompt, images)},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        response = requests.post(
            config["base_url"] + "/chat/completions",
            headers=headers, json=payload, timeout=config["timeout"],
        )
        if response.status_code >= 400:
            raise EpcAiError("HTTP %s: %s" % (response.status_code, response.text[:500]))
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise EpcAiError("Unexpected response format: %s" % str(data)[:500]) from exc

    @api.model
    def _call_anthropic(self, config, system, prompt, max_tokens, images=None):
        headers = {
            "Content-Type": "application/json",
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": config["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": self._anthropic_content(prompt, images)}],
            "temperature": 0.2,
        }
        response = requests.post(
            config["base_url"] + "/messages",
            headers=headers, json=payload, timeout=config["timeout"],
        )
        if response.status_code >= 400:
            raise EpcAiError("HTTP %s: %s" % (response.status_code, response.text[:500]))
        data = response.json()
        try:
            return "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise EpcAiError("Unexpected response format: %s" % str(data)[:500]) from exc

    @api.model
    def _openai_content(self, prompt, images):
        if not images:
            return prompt
        content = [{"type": "text", "text": prompt}]
        for mime, data in images:
            content.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data)}})
        return content

    @api.model
    def _anthropic_content(self, prompt, images):
        if not images:
            return prompt
        content = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                   for mime, data in images]
        content.append({"type": "text", "text": prompt})
        return content

    @api.model
    def _complete(self, system, prompt, max_tokens=1500, images=None):
        config = self._get_config()
        try:
            if config["provider"] == "anthropic":
                return self._call_anthropic(config, system, prompt, max_tokens, images=images)
            return self._call_openai(config, system, prompt, max_tokens, images=images)
        except requests.RequestException as exc:
            raise EpcAiError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def parse_json(self, text):
        """Extract the first JSON object or array from an LLM answer."""
        if not text:
            raise ValueError("empty answer")
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        try:
            return json.loads(cleaned)
        except ValueError:
            pass
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("no JSON found in answer")
        return json.loads(match.group(1))

    @api.model
    def _log(self, values):
        try:
            self.env["epc.ai.log"].sudo().create(values)
        except Exception:  # logging must never break the business flow
            _logger.exception("Could not write EPC AI log")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    @api.model
    def ask(self, feature, system, prompt, fallback, record=None, expect_json=False, max_tokens=1500,
            images=None):
        """Ask the LLM, or use ``fallback`` when AI is disabled or fails.

        :param feature: technical name of the feature, for the audit log
        :param fallback: callable without arguments returning the same shape
            as the expected answer (text, or parsed JSON when ``expect_json``)
        :return: dict ``{"result": ..., "source": "llm" | "fallback", "error": str}``
        """
        config = self._get_config()
        log_vals = {
            "feature": feature,
            "provider": config["provider"],
            "model_name": config["model"],
            "res_model": record._name if record else False,
            "res_id": record.id if record and len(record) == 1 else False,
            "prompt": ((prompt or "") + ("\n[%s image(s) attached]" % len(images) if images else ""))[:MAX_LOG_CHARS],
        }
        error = ""
        if self.is_enabled() and not self.env.context.get("epc_ai_force_fallback"):
            started = time.monotonic()
            try:
                answer = self._complete(system, prompt, max_tokens=max_tokens, images=images)
                result = self.parse_json(answer) if expect_json else answer.strip()
                if not expect_json and not result:
                    raise ValueError("empty answer")
                log_vals.update({
                    "response": (answer or "")[:MAX_LOG_CHARS],
                    "success": True,
                    "used_fallback": False,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                })
                self._log(log_vals)
                return {"result": result, "source": "llm", "error": ""}
            except (EpcAiError, ValueError) as exc:
                error = str(exc)[:2000]
                _logger.warning("EPC AI feature %s failed, using fallback: %s", feature, error)
                log_vals["duration_ms"] = int((time.monotonic() - started) * 1000)
        result = fallback()
        log_vals.update({
            "response": (json.dumps(result, default=str) if expect_json else str(result))[:MAX_LOG_CHARS],
            "success": not error,
            "used_fallback": True,
            "error": error,
        })
        self._log(log_vals)
        return {"result": result, "source": "fallback", "error": error}

    @api.model
    def test_connection(self):
        """Used by the settings button. Returns (ok, message)."""
        if not self.is_enabled():
            return False, "AI provider is not configured; the built-in engine is used."
        try:
            answer = self._complete("You are a connection test.", "Reply with the single word OK.", max_tokens=10)
        except EpcAiError as exc:
            return False, str(exc)
        return True, "Connected. Model answered: %s" % (answer or "").strip()[:100]
