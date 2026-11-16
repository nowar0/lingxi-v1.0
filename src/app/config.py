"""配置与密钥。

配置：从 config/models.yaml 读（哪种模型、地址在哪）
密钥：交给 Windows 凭据管理器保管（keyring），代码只负责取出来用
"""

import os
import sys
from pathlib import Path

import keyring
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
BUNDLED_MODEL_ROOT = Path(os.environ.get("LINGXI_BUNDLED_MODEL_ROOT") or (Path(sys.executable).resolve().parents[2] / "models" if getattr(sys, "frozen", False) else (BASE_DIR / "models" if (BASE_DIR / "models").is_dir() else BASE_DIR.parent / "models")))
CONFIG_PATH = BASE_DIR / "config" / "models.yaml"
SERVICE = "lingxi-v1-app"  # 凭据管理器里的"抽屉名"


# 数据放哪：优先放系统 AppData（打包成 exe 后，程序目录是只读的）
APPDATA = os.environ.get("APPDATA")
DATA_DIR = Path(os.environ.get("LINGXI_DATA_DIR") or (
    str(Path(APPDATA) / "LingxiV1") if APPDATA else str(BASE_DIR / "data")
))
DB_PATH = DATA_DIR / "lingxi.db"


def load_models():
    """读出 config/models.yaml"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_key(provider_id, key):
    """先使旧代理批准失效，再将Key存入系统凭据管理器；不持久Key摘要。"""
    from app.credential_generation import default_store
    default_store().mutate(
        provider_id, 'save', lambda: keyring.set_password(SERVICE, provider_id, key))


def delete_key(provider_id):
    """删除与补偿都推进用户级安全代际；已不存在也标记deleted。"""
    from app.credential_generation import default_store
    def remove():
        try:
            keyring.delete_password(SERVICE, provider_id)
        except keyring.errors.PasswordDeleteError:
            pass
    return default_store().mutate(provider_id, 'delete', remove)


def load_key(provider_id):
    """取出 key，没存过就返回 None"""
    return keyring.get_password(SERVICE, provider_id)


def think_enabled(role_or_model):
    """这个角色要不要让模型"想"。

    只有 models.yaml 的 roles 里的角色名才配得到开关；
    直接写模型全名的一律走默认值（想）。
    默认"想"的理由见 models.yaml 里的注释。
    """
    cfg = load_models()
    return (cfg.get("think") or {}).get(role_or_model, True)
