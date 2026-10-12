from llm_utils import LLMClient, get_client, RateLimiter, LLMProvider as GeminiTier
import os

# Compatibility layer for legacy Gemini-only code
class GeminiClient(LLMClient):
    def __init__(self, api_key=None):
        # In llm_utils, api_key is the 2nd argument, or we can use keyword
        super().__init__(api_key=api_key)

def get_env_path():
    from llm_utils import get_env_path
    return get_env_path()

_CLIENT = None
def get_client():
    global _CLIENT
    if not _CLIENT:
        _CLIENT = GeminiClient()
    return _CLIENT