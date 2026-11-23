"""You.com Search plugin — bundled, auto-loaded.

``provider.py`` holds the provider class, ``register(ctx)`` registers an instance.
"""

from __future__ import annotations

from plugins.web.youcom.provider import YouComWebSearchProvider


def register(ctx) -> None:
    """Register the You.com search provider with the plugin context."""
    ctx.register_web_search_provider(YouComWebSearchProvider())
