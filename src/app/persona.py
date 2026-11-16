"""人设配置原子保存；文件损坏时保留原文件并回退默认值。"""

import json
import logging
import os
import tempfile

from app.config import BASE_DIR, DATA_DIR

DEFAULT_PATH = BASE_DIR / "config" / "persona_default.txt"
USER_PATH = DATA_DIR / "persona.json"
DEFAULT_NAME = "零汐"


def default_body():
    return DEFAULT_PATH.read_text(encoding="utf-8")


def load_persona():
    persona = {"name": DEFAULT_NAME, "nickname": "", "body": default_body()}
    if USER_PATH.exists():
        try:
            saved = json.loads(USER_PATH.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("人设文件必须是对象")
            if any(not isinstance(saved.get(k, ""), str) for k in persona):
                raise ValueError("人设字段必须是文字")
            for k in persona:
                if k in saved and (saved[k].strip() or k == "nickname"):
                    persona[k] = saved[k]
        except (OSError, ValueError) as exc:
            logging.warning("人设文件无法读取，使用默认值：%s", exc)
            persona["warning"] = "人设文件损坏，当前使用默认值；原文件仍保留。可在设置中重新保存。"
    return persona


def save_persona(name, nickname, body):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "nickname": nickname, "body": body}
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA_DIR,
                                         prefix="persona-", suffix=".tmp", delete=False) as f:
            temp = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, USER_PATH)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def render(persona):
    text = persona["body"]
    text = text.replace("{agent_name}", persona.get("name", "").strip() or DEFAULT_NAME)
    return text.replace("{user_nickname}", persona.get("nickname", "").strip() or "你")
