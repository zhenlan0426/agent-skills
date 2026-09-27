"""Small synchronous API; no benchmark uploads, chat state, or tool execution."""
import json
import math
import re
from urllib.parse import urlsplit

import httpx
import jsonschema

from .auth import Credentials, KaggleLLMError
from .jsonutil import loads


def _slug(model):
    return model.split("/", 1)[-1].replace("@", "-")


def resolve_model(model, values):
    model = model or values.get("LLM_DEFAULT")
    if not isinstance(model, str) or not model.strip():
        raise KaggleLLMError("No model configured; provide model= or run kaggle-llm auth.")
    if "/" in model:
        # The CLI's curated list can lag actual proxy access. Exact IDs are
        # forwarded unchanged; Kaggle remains the authority on availability.
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", model):
            raise ValueError("Use an exact provider/model ID without whitespace")
        return model
    available = [s.strip() for s in (values.get("LLMS_AVAILABLE") or "").split(",") if s.strip()]
    if model in available or not available:
        return model
    matches = [s for s in available if _slug(s) == _slug(model)]
    if len(matches) == 1:
        return matches[0]
    raise KaggleLLMError(f"Unknown model alias {model!r}. Run kaggle-llm models or provide the exact provider/model ID.")


def _endpoint(value):
    url = value.rstrip("/")
    for suffix in ("/openapi", "/genai"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
            break
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise KaggleLLMError("MODEL_PROXY_URL must be an HTTPS base URL without user info, query, or fragment.")
    return url + "/openapi/chat/completions"


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

    def chat(self, messages, *, model=None, max_tokens=None, temperature=None,
             reasoning=None, response_format=None):
        """Return a raw Chat Completions dict. Text messages only; no streaming.

        401 refreshes once. 403, 429, 5xx and timeouts are surfaced without retry,
        avoiding accidental duplicate work and hidden quota spending.
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
        payload = {"messages": messages, "stream": False}
        for key, value in (("max_tokens", max_tokens), ("temperature", temperature),
                           ("reasoning_effort", reasoning), ("response_format", response_format)):
            if value is not None:
                payload[key] = value
        values = self.credentials.ensure()
        for attempt in range(2):
            payload["model"] = resolve_model(model, values)
            try:
                response = self.http.post(
                    _endpoint(values["MODEL_PROXY_URL"]), json=payload,
                    headers={"Authorization": "Bearer " + values["MODEL_PROXY_API_KEY"]},
                )
            except httpx.TimeoutException:
                raise KaggleLLMError("Inference timed out; the server may have processed it. No automatic retry.") from None
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
                raise KaggleLLMError(f"HTTP {response.status_code}: {hint}")
            try:
                result = response.json()
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
            prompt += "\n\nReturn only valid JSON satisfying this schema (no markdown):\n" + json.dumps(schema, allow_nan=False)
            if schema_mode == "native":
                options["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "result", "strict": True, "schema": schema,
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
            if not isinstance(text, str) or not text.strip():
                raise KaggleLLMError("Model returned no text content.")
        except (TypeError, KeyError, IndexError, AttributeError):
            raise KaggleLLMError("Malformed completion response.") from None
        # Kaggle may embed reasoning in content when effort is requested.
        if options.get("reasoning") not in (None, "none"):
            text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()
        structured = None
        if schema is not None:
            candidate = text.strip()
            if candidate.startswith("```") and candidate.endswith("```"):
                candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate)
            try:
                structured = loads(candidate)
                validator_cls(schema).validate(structured)
            except (ValueError, jsonschema.ValidationError):
                raise KaggleLLMError("Model output is invalid JSON or does not match the supplied schema.") from None
            except Exception:
                raise KaggleLLMError("JSON Schema validation failed; check local references and schema compatibility.") from None
        return {"text": text, "structured_output": structured, "model": raw.get("model"),
                "usage": raw.get("usage", {}), "finish_reason": finish, "id": raw.get("id")}
