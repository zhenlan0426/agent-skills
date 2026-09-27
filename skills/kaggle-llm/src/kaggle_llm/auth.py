"""Isolated credential bootstrap with cross-process refresh locking (Unix)."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta

from dotenv import dotenv_values


class KaggleLLMError(RuntimeError):
    """A safe-to-display failure; excludes credentials and upstream request bodies."""

    def __init__(self, message, *, batch_fatal=False):
        super().__init__(message)
        self.batch_fatal = batch_fatal


def default_env_file():
    return Path(os.environ.get("KAGGLE_LLM_ENV_FILE", "~/.config/kaggle-llm/credentials.env")).expanduser()


class Credentials:
    def __init__(self, path=None):
        self.path = Path(path).expanduser().absolute() if path else default_env_file().absolute()

    def read(self):
        # Deliberately don't load the caller's .env or mutate os.environ.
        return dict(dotenv_values(self.path, interpolate=False)) if self.path.exists() else {}

    @staticmethod
    def valid(values):
        if not values.get("MODEL_PROXY_URL") or not values.get("MODEL_PROXY_API_KEY"):
            return False
        expiry = values.get("MODEL_PROXY_EXPIRY_TIME")
        if not expiry:
            return True
        try:
            when = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when > datetime.now(timezone.utc) + timedelta(seconds=60)
        except ValueError:
            return False

    def ensure(self, *, force=False, rejected_token=None):
        values = self.read()
        if not force and self.valid(values):
            return values
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            values = self.read()
            # Another worker may already have replaced the rejected credential.
            changed = rejected_token and values.get("MODEL_PROXY_API_KEY") != rejected_token
            if self.valid(values) and (not force or changed):
                return values
            executable = Path(sys.executable).parent / "kaggle"
            command = str(executable) if executable.exists() else shutil.which("kaggle")
            if not command:
                raise KaggleLLMError("Kaggle CLI is missing. Install kaggle>=2.2.2 and authenticate your Kaggle account.", batch_fatal=True)
            with tempfile.TemporaryDirectory(prefix="refresh-", dir=self.path.parent) as directory:
                env = Path(directory) / "credentials.env"
                try:
                    result = subprocess.run(
                        [command, "b", "init", "-y", "--env-file", str(env),
                         "--example-file", str(Path(directory) / "example.py")],
                        cwd=directory, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise KaggleLLMError("Kaggle credential refresh failed or timed out; check Kaggle CLI connectivity.", batch_fatal=True) from None
                if result.returncode or not env.exists():
                    # CLI errors can include tokens or URLs: don't relay them verbatim.
                    raise KaggleLLMError(
                        "Kaggle credential refresh failed. Check your Kaggle login, account verification, "
                        "and Benchmarks access. Diagnose with `kaggle b init -y` in a private temporary directory.",
                        batch_fatal=True,
                    )
                values = dict(dotenv_values(env, interpolate=False))
                if not self.valid(values):
                    raise KaggleLLMError("Kaggle returned missing, expired, or malformed proxy credentials.", batch_fatal=True)
                env.chmod(0o600)
                os.replace(env, self.path)
                return values

    def status(self, *, refresh=False):
        values = self.ensure(force=refresh)
        return {
            "env_file": str(self.path),
            "expires_at": values.get("MODEL_PROXY_EXPIRY_TIME"),
            "default_model": values.get("LLM_DEFAULT"),
            "models": [s.strip() for s in (values.get("LLMS_AVAILABLE") or "").split(",") if s.strip()],
            "models_source": "Kaggle CLI curated LLMS_AVAILABLE; not exhaustive or live-verified",
            "note": "Exact provider/model IDs can be called even if unlisted; the proxy decides access.",
        }
