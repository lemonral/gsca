import json
import httpx
from .config import Settings


class ProviderError(Exception):
    pass


class OllamaProvider:
    """Uses the existing authenticated /api/chat proxy, not OpenAI's API."""
    def __init__(self, settings: Settings):
        self.settings = settings

    def headers(self):
        token = self.settings.read_model_token()
        return {"Authorization": "Bearer " + token} if token else {}

    async def health(self):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(self.settings.model_base_url + "/health", headers=self.headers())
                response.raise_for_status()
                return response.json()
        except (OSError, ValueError, httpx.HTTPError):
            raise ProviderError("The Qwen proxy is unavailable or authentication failed. Check the model configuration.") from None

    async def chat(self, messages, tools, remaining_tokens):
        payload = {
            "model": self.settings.model_name, "messages": messages, "tools": tools,
            "stream": False, "think": False,
            "options": {"temperature": 0.1, "num_ctx": self.settings.context_tokens,
                        "num_predict": min(self.settings.max_output_tokens, remaining_tokens)},
        }
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(min(230, self.settings.run_timeout), connect=8)) as client:
                async with client.stream("POST", self.settings.model_base_url + "/api/chat",
                                         json=payload, headers=self.headers()) as response:
                    if response.status_code != 200:
                        raise ProviderError(f"The Qwen proxy returned HTTP {response.status_code}. The request was not retried automatically.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ProviderError("The model response exceeds the size limit.")
            data = json.loads(body)
            if not data.get("done") or not isinstance(data.get("message"), dict):
                raise ProviderError("The model did not return a complete result.")
            if data.get("done_reason") == "length":
                raise ProviderError("The model reached the output limit. Reduce the task or increase the output budget.")
            message = data["message"]
            content = message.get("content", "")
            calls = message.get("tool_calls", [])
            if not isinstance(content, str) or not isinstance(calls, list):
                raise ProviderError("The model returned an invalid message structure.")
            # Deliberately omit the provider's optional internal thinking field.
            return {"role": "assistant", "content": content, "tool_calls": calls}, {
                "prompt_tokens": int(data.get("prompt_eval_count", 0)),
                "completion_tokens": int(data.get("eval_count", 0)),
            }
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("The Qwen request timed out. It was not resubmitted automatically.") from None
        except (OSError, ValueError, TypeError, httpx.HTTPError):
            raise ProviderError("The Qwen connection or response parsing failed. Check the service status.") from None
