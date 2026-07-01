import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.dashscope_api_key:
            raise ValueError("请在 .env 中设置 DASHSCOPE_API_KEY")
        self.settings = settings
        self.client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.dashscope_base_url, timeout=60.0)

    def chat(self, model: str, messages: list[dict[str, Any]], temperature: float, max_tokens: int) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = self.client.chat.completions.create(**kwargs)
        content = (resp.choices[0].message.content or "").strip()
        if not content and "deepseek" in self.settings.dashscope_base_url.lower():
            retry_kwargs = dict(kwargs)
            retry_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            resp = self.client.chat.completions.create(**retry_kwargs)
            content = (resp.choices[0].message.content or "").strip()
        return content

    def chat_with_image(
        self,
        model: str,
        prompt: str,
        image_path: str,
        temperature: float = 0.1,
        max_tokens: int = 1200,
    ) -> str:
        path = Path(image_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        data_url = f"data:{mime_type};base64,{data}"
        return self.chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )


def parse_json_object(text: str) -> dict[str, Any]:
    # Strip qwen3 / reasoning-model <think>...</think> blocks before parsing
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))
