"""References name entries in the OS credential store; they are never API keys."""


def looks_like_secret(value):
    return len(value) > 20 and value.startswith(('sk-', 'AIza', 'Bearer '))


def validate_reference(value):
    if looks_like_secret(value):
        raise ValueError('凭据引用不能填写 API Key；请在 API Key 输入框保存密钥')
    return value
