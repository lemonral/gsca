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
            raise ProviderError("千问代理不可用或鉴权失败，请检查服务器模型配置。") from None

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
                        raise ProviderError(f"千问代理返回HTTP {response.status_code}，本次未自动重试。")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ProviderError("模型响应超过大小限制。")
            data = json.loads(body)
            if not data.get("done") or not isinstance(data.get("message"), dict):
                raise ProviderError("模型未返回完整结果。")
            if data.get("done_reason") == "length":
                raise ProviderError("模型输出达到长度上限，请缩小任务或提高输出预算。")
            message = data["message"]
            content = message.get("content", "")
            calls = message.get("tool_calls", [])
            if not isinstance(content, str) or not isinstance(calls, list):
                raise ProviderError("模型返回了无效消息结构。")
            # Deliberately omit the provider's optional internal thinking field.
            return {"role": "assistant", "content": content, "tool_calls": calls}, {
                "prompt_tokens": int(data.get("prompt_eval_count", 0)),
                "completion_tokens": int(data.get("eval_count", 0)),
            }
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("千问响应超时，本次未自动重复提交。") from None
        except (OSError, ValueError, TypeError, httpx.HTTPError):
            raise ProviderError("千问接口连接或响应解析失败，请检查服务状态。") from None
