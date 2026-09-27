"""Stateless LLM calls using your Kaggle account's local Model Proxy access."""
from .client import Client, KaggleLLMError

__all__ = ["Client", "KaggleLLMError"]
