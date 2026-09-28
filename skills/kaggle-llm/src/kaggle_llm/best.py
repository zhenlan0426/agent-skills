"""Pick the newest, strongest model this account can actually call.

Kaggle's curated LLMS_AVAILABLE is hard-coded in the Kaggle CLI and lags the
proxy, while the benchmark catalog lists models this account cannot call
locally. So candidates come from both, are ranked by preference, and the first
one that answers a tiny probe wins. The pick is cached next to the credentials.
"""
import contextlib
import io
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

CACHE_TTL = timedelta(hours=24)
PROBE_MESSAGES = [{"role": "user", "content": "Reply with OK."}]
# Statuses meaning "not callable with this account"; anything else aborts selection.
UNAVAILABLE = (400, 403, 404)

# Claude = OpenAI > Gemini. Other providers and open-weight models are not candidates.
TIER = {"anthropic": 0, "openai": 0, "google": 1}
FAMILY = {
    "anthropic": re.compile(r"claude-(?P<size>opus|sonnet|haiku)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?"),
    "openai": re.compile(r"gpt-(?P<major>\d+)(?:\.(?P<minor>\d+))?(?P<size>-.*)?"),
    "google": re.compile(r"gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-(?P<size>.+)"),
}


def _size(text):
    text = text or ""
    if any(word in text for word in ("haiku", "nano", "lite")):
        return 2
    if any(word in text for word in ("sonnet", "mini", "flash")):
        return 1
    return 0  # opus, pro, and full-size GPT releases


def rank_key(slug):
    """Sort key (lower is better), or None when the slug is not a candidate.

    Order: provider tier, then newest major version, then largest size, then
    newest minor version. Size sits before the minor version so that across
    Claude and OpenAI a small model with a higher point number (gpt-5.4-nano)
    does not outrank a larger one of the same generation (claude-sonnet-5).
    """
    provider, _, name = slug.partition("/")
    pattern = FAMILY.get(provider)
    match = pattern and pattern.fullmatch(name.split("@", 1)[0])
    if not match:
        return None
    return (TIER[provider], -int(match["major"]), _size(match["size"]),
            -int(match["minor"] or 0), "preview" in name, list(FAMILY).index(provider), slug)


def rank(slugs):
    return sorted({s for s in slugs if rank_key(s)}, key=rank_key)


def fetch_catalog():
    """Proxy slugs from Kaggle's live benchmark model list, or None if unreachable."""
    try:
        # The Kaggle client authenticates on import and may print; keep stdout clean.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi
            from kagglesdk.benchmarks.types.benchmarks_api_service import ApiListBenchmarkModelsRequest
            api = KaggleApi()
            api.authenticate()
            now = datetime.now(timezone.utc)
            slugs, token = [], ""
            with api.build_kaggle_client() as kaggle:
                while True:
                    request = ApiListBenchmarkModelsRequest()
                    if token:
                        request.page_token = token
                    response = kaggle.benchmarks.benchmarks_api_client.list_benchmark_models(request)
                    for model in response.benchmark_models or []:
                        version = model.version
                        deprecated = version and version.deprecation_time
                        if deprecated and deprecated.tzinfo is None:
                            deprecated = deprecated.replace(tzinfo=timezone.utc)
                        if (version and version.allow_model_proxy and version.model_proxy_slug
                                and version.published and not (deprecated and deprecated <= now)):
                            slugs.append(version.model_proxy_slug)
                    token = response.next_page_token or ""
                    if not token:
                        return slugs
    except (Exception, SystemExit):
        return None


def cache_path(credentials):
    return credentials.path.with_suffix(credentials.path.suffix + ".model.json")


def read_cache(credentials):
    try:
        cached = json.loads(cache_path(credentials).read_text())
        selected = datetime.fromisoformat(cached["selected_at"])
        if isinstance(cached.get("model"), str) and datetime.now(timezone.utc) - selected < CACHE_TTL:
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def forget_if(credentials, model):
    """Drop the cached pick if it is the model the proxy just rejected."""
    cached = read_cache(credentials)
    if cached and cached["model"] == model:
        with contextlib.suppress(FileNotFoundError):
            cache_path(credentials).unlink()


def select(client, *, refresh=False):
    """Return the cached pick, or probe ranked candidates and cache the first that answers."""
    from .auth import KaggleLLMError
    from .client import finish

    if not refresh and (cached := read_cache(client.credentials)):
        return cached
    values = client.credentials.ensure()
    listed = [s.strip() for s in (values.get("LLMS_AVAILABLE") or "").split(",") if s.strip()]
    catalog = fetch_catalog()
    candidates = rank(listed + (catalog or []))
    unavailable = []
    for model in candidates:
        try:
            raw = client.chat(PROBE_MESSAGES, model=model, max_tokens=256)
        except KaggleLLMError as exc:
            if exc.status in UNAVAILABLE:
                unavailable.append(model)
                continue
            raise
        try:
            finish(raw, None)
        except KaggleLLMError:
            unavailable.append(model)
            continue
        result = {
            "model": model,
            "selected_at": datetime.now(timezone.utc).isoformat(),
            "catalog_source": "kaggle benchmark models + LLMS_AVAILABLE" if catalog is not None
                              else "LLMS_AVAILABLE only (catalog unreachable)",
            "unavailable": unavailable,
            "untried": candidates[len(unavailable) + 1:],
        }
        path = cache_path(client.credentials)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
            json.dump(result, handle, indent=1)
        os.replace(handle.name, path)
        return result
    raise KaggleLLMError(f"No Claude, OpenAI, or Gemini model answered; tried {len(candidates)}.",
                         batch_fatal=True)
