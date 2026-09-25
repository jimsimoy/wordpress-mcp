"""Registry of plugin settings adapters. Add new adapters here."""

from .base import SettingsAdapter, SettingsError
from .wcdn import WcdnAdapter

ADAPTERS = {a.slug: a for a in (WcdnAdapter(),)}


def adapter_slugs():
    return sorted(ADAPTERS)


def get_adapter(slug):
    try:
        return ADAPTERS[slug]
    except KeyError:
        raise SettingsError(f"No adapter for plugin {slug!r}. Available: {', '.join(adapter_slugs())}")
