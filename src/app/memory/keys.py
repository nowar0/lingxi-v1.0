"""键名整理：把同一个意思的不同说法，整理成同一个名字。

规则只写这一处 —— 以后要加别名，就改这张表。
"""

# 左边是"规范名"，右边是它的各种别名（都归到左边这个规范名）
_CANON = {
    "喜欢的颜色": ["颜色", "我喜欢的颜色", "测试用的颜色", "喜好颜色"],
}


def normalize_key(key):
    key = key.strip()
    for canon, aliases in _CANON.items():
        if key == canon or key in aliases:
            return canon
    return key
