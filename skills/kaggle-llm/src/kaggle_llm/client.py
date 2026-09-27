"""Small synchronous API; no benchmark uploads, chat state, or tool execution."""
import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jsonschema

from . import best
from .auth import Credentials, KaggleLLMError
from .jsonutil import loads


def _slug(model):
    return model.split("/", 1)[-1].replace("@", "-")


def resolve_model(model, values):
    if not isinstance(model, str) or not model.strip():
        raise KaggleLLMError("No model given; pass model= or leave it None for best_model().", batch_fatal=True)
    if "/" in model:
        # The CLI's curated list can lag actual proxy access. Exact IDs are
        # forwarded unchanged; Kaggle remains the authority on availability.
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", model):
            raise KaggleLLMError("Use an exact provider/model ID without whitespace", batch_fatal=True)
        return model
    available = [s.strip() for s in (values.get("LLMS_AVAILABLE") or "").split(",") if s.strip()]
    if model in available:
        return model
    matches = [s for s in available if (
        _slug(s) == _slug(model) or s.split("/", 1)[-1].split("@", 1)[0] == model
    )]
    if len(matches) == 1:
        return matches[0]
    raise KaggleLLMError(f"Unknown model alias {model!r}. Run kaggle-llm models or provide the exact provider/model ID.", batch_fatal=True)


def _endpoint(value):
    url = value.rstrip("/")
    for suffix in ("/openapi", "/genai"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
            break
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                 and parsed.username is None and not parsed.query and not parsed.fragment
                 and not re.search(r"[\s\\]", url))
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise KaggleLLMError("MODEL_PROXY_URL must be an HTTPS base URL without user info, query, or fragment.", batch_fatal=True)
    return url + "/openapi/chat/completions"


@dataclass(frozen=True)
class _PreparedSchema:
    schema: object
    validator: object
    encoded: str


def _prepare_schema(schema):
    if isinstance(schema, _PreparedSchema):
        return schema
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
    except (jsonschema.SchemaError, TypeError, AttributeError):
        raise ValueError("Invalid JSON Schema") from None

    # Keep validation offline: allow only same-document references.
    def check_refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$ref", "$dynamicRef") and isinstance(value, str) and not value.startswith("#"):
                    raise ValueError("Only local JSON Schema references are supported")
                check_refs(value)
        elif isinstance(node, list):
            for item in node:
                check_refs(item)
    check_refs(schema)
    return _PreparedSchema(schema, validator_cls(schema), json.dumps(schema, allow_nan=False))


class Client:
    def __init__(self, *, env_file=None, timeout=120, transport=None):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.credentials = Credentials(env_file)
        self.http = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def models(self):
        return self.credentials.status()

    def best_model(self, *, refresh=False):
        """Preferred callable model: Claude = OpenAI > Gemini, newest first. Cached 24h."""
        return best.select(self, refresh=refresh)["model"]

    def chat(self, messages, *, model=None, max_tokens=None, temperature=None,
             reasoning=None, response_format=None):
        """Return a raw Chat Completions dict. Text messages only; no streaming.

        model=None uses best_model(). 401 refreshes once. 403, 429, 5xx and timeouts
        are surfaced without retry, avoiding accidental duplicate work and hidden
        quota spending.
        """
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty list")
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] not in ("system", "user", "assistant")
                    or not isinstance(message["content"], str)):
                raise ValueError("Each message must contain only role (system/user/assistant) and text content")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        if temperature is not None and (not isinstance(temperature, (int, float))
                                       or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError("temperature must be between 0 and 2")
        if reasoning is not None and reasoning not in ("none", "minimal", "low", "medium", "high"):
            raise ValueError("Unsupported reasoning effort")
        if model is None:
            model = self.best_model()
        payload = {"messages": messages, "stream": False}
        for key, value in (("max_tokens", max_tokens), ("temperature", temperature),
                           ("reasoning_effort", reasoning), ("response_format", response_format)):
            if value is not None:
                payload[key] = value
        # Reject bad local configuration before a potentially costly refresh.
        # Empty/missing credentials still need first-use bootstrap for aliases.
        current = self.credentials.read()
        if current or (isinstance(model, str) and "/" in model):
            resolve_model(model, current)
        if current.get("MODEL_PROXY_URL"):
            _endpoint(current["MODEL_PROXY_URL"])
        values = self.credentials.ensure()
        for attempt in range(2):
            payload["model"] = resolve_model(model, values)
            try:
                response = self.http.post(
                    _endpoint(values["MODEL_PROXY_URL"]), json=payload,
                    headers={"Authorization": "Bearer " + values["MODEL_PROXY_API_KEY"]},
                )
            except httpx.TimeoutException:
                raise KaggleLLMError("Inference timed out; the server may have processed it. No automatic retry.", batch_transient=True) from None
            except httpx.InvalidURL:
                raise KaggleLLMError("Invalid MODEL_PROXY_URL.", batch_fatal=True) from None
            except httpx.HTTPError:
                raise KaggleLLMError("Inference connection failed. No automatic retry.") from None
            if response.status_code == 401 and attempt == 0:
                values = self.credentials.ensure(force=True, rejected_token=values["MODEL_PROXY_API_KEY"])
                continue
            if response.status_code != 200:
                hints = {
                    400: "The model rejected the request or an optional parameter.",
                    401: "Authentication failed after one credential refresh.",
                    403: "Account or model access denied; credentials were not refreshed.",
                    404: "Model or proxy endpoint unavailable.",
                    429: "Quota or rate limit reached. Wait before trying again.",
                }
                hint = hints.get(response.status_code, "Proxy request failed; no automatic retry.")
                if response.status_code in (403, 404):
                    best.forget_if(self.credentials, payload["model"])  # Reselect on the next call.
                raise KaggleLLMError(f"HTTP {response.status_code}: {hint}", status=response.status_code,
                                     batch_fatal=response.status_code in (401, 403, 404, 429),
                                     batch_transient=500 <= response.status_code < 600)
            try:
                result = loads(response.text)
            except ValueError:
                raise KaggleLLMError("Proxy returned invalid JSON.") from None
            if not isinstance(result, dict) or not result.get("choices"):
                raise KaggleLLMError("Proxy returned no completion choices.")
            return result

    def prompt(self, prompt, *, system=None, schema=None, schema_mode="prompt", **options):
        """Return text, structured_output, model, usage and finish_reason.

        schema_mode='prompt' requests JSON in the prompt and validates locally.
        'native' additionally sends response_format=json_schema (model dependent).
        Invalid/truncated output raises; it is never silently accepted or retried.
        """
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a nonempty string")
        if system is not None and not isinstance(system, str):
            raise ValueError("system must be text")
        if schema_mode not in ("prompt", "native"):
            raise ValueError("schema_mode must be prompt or native")
        if schema is not None:
            prepared = _prepare_schema(schema)
            prompt += "\n\nReturn only valid JSON satisfying this schema (no markdown):\n" + prepared.encoded
            if schema_mode == "native":
                options["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "result", "strict": True, "schema": prepared.schema,
                }}
        messages = ([{"role": "system", "content": system}] if system is not None else [])
        messages.append({"role": "user", "content": prompt})
        raw = self.chat(messages, **options)
        try:
            choice = raw["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason")
            if message.get("refusal") or finish == "content_filter":
                raise KaggleLLMError("Model refused or filtered the request.")
            if finish == "length":
                raise KaggleLLMError("Output was truncated; increase max_tokens before retrying.")
            text = message.get("content")
            # Some models include reasoning by default, even without an effort option.
            if isinstance(text, str):
                text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.S)
            if not isinstance(text, str) or not text.strip():
                raise KaggleLLMError("Model returned no text content.")
        except (TypeError, KeyError, IndexError, AttributeError):
            raise KaggleLLMError("Malformed completion response.") from None
        structured = None
        if schema is not None:
            candidate = text.strip()
            if candidate.startswith("```") and candidate.endswith("```"):
                candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
            try:
                structured = loads(candidate)
                prepared.validator.validate(structured)
            except (ValueError, jsonschema.ValidationError):
                raise KaggleLLMError("Model output is invalid JSON or does not match the supplied schema.") from None
            except Exception:
                raise KaggleLLMError("JSON Schema validation failed; check local references and schema compatibility.") from None
        return {"text": text, "structured_output": structured, "model": raw.get("model"),
                "usage": raw.get("usage", {}), "finish_reason": finish, "id": raw.get("id")}
