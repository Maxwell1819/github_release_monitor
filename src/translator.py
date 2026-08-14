"""百度翻译（自动分段）"""

import hashlib
import logging
import random
from typing import Optional

import requests

logger = logging.getLogger("ReleaseMonitor")


class Translator:
    API = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def __init__(self, app_id: str, secret_key: str):
        self.app_id = app_id
        self.secret_key = secret_key

    def translate(self, text: str) -> Optional[str]:
        if not text.strip():
            return text
        # 单次请求长度控制在 5000 字符内（API 上限 6000），超长则在段落边界分段
        if len(text) > 5000:
            idx = text.rfind("\n\n", 0, 5000)
            if idx < 0:
                idx = 5000
            first = self._call(text[:idx])
            if not first:
                return None
            rest = self.translate(text[idx:])
            if rest is None:
                return None  # 后续段失败 → 整体失败，避免静默截断
            sep = "\n\n" if text[idx:idx + 2] == "\n\n" else ""
            return first + sep + rest
        return self._call(text)

    def _call(self, text: str) -> Optional[str]:
        salt = random.randint(32768, 65536)
        raw = f"{self.app_id}{text}{salt}{self.secret_key}"
        sign = hashlib.md5(raw.encode()).hexdigest()
        try:
            resp = requests.get(self.API, params={
                "q": text, "from": "auto", "to": "zh",
                "appid": self.app_id, "salt": salt, "sign": sign,
            }, timeout=10)
            data = resp.json()
            if data.get("error_code"):
                logger.warning(f"翻译 API 错误: {data.get('error_msg', data['error_code'])}")
                return None
            return "\n".join(item["dst"] for item in data.get("trans_result", []))
        except Exception as e:
            logger.warning(f"翻译请求失败: {e}")
            return None