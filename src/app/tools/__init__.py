"""Compatibility exports derived from the unified capability registry."""
from app.tools.registry import registry

TOOLS = registry.tools
BY_NAME = registry.by_name
SPECS = [tool['spec'] for tool in TOOLS]


def specs_for(ctx):
    return [item.spec for item in registry.visible(ctx)]
