"""
Plugin settings adapters.

Some plugins keep their settings behind their own REST routes rather than in anything the core
`wp/v2` API exposes, and those routes rarely behave like a simple "PATCH these keys". An adapter is
the place where one plugin's route shape and its gotchas live, behind a common interface, so an
agent asks for "set these keys" and the adapter does the read-modify-write safely.

To support another plugin: subclass SettingsAdapter, implement groups/get/update, and register an
instance in plugin_settings/__init__.py. See wcdn.py for a worked example, and tests/ for how to test
one against a fake client with no network.

Contract for update(): dry_run defaults to True; a dry run makes only the reads it needs and writes
nothing; a real run verifies what it wrote from the plugin's own save responses and says so.
"""

import difflib


class SettingsError(Exception):
    """A problem the caller can fix (bad group, unknown key, wrong type, plugin said no)."""


class SettingsAdapter:
    slug = ""          # short id used as the `plugin` argument, e.g. "wcdn"
    title = ""         # human name shown in tool descriptions
    description = ""   # what it covers, which groups exist, what to know before writing

    def groups(self):
        """Names accepted as `group` / keys of `changes`."""
        raise NotImplementedError

    def get(self, client, group=None, keys=None):
        raise NotImplementedError

    def update(self, client, changes, dry_run=True, **options):
        raise NotImplementedError


def check_type(group, key, current, new):
    """Reject a value whose type cannot match the setting's current one. Plugins sanitise silently, so a
    wrong-typed value is otherwise stored as something else with no error."""
    if current is None:
        return
    expected = (
        "boolean" if isinstance(current, bool)
        else "number" if isinstance(current, (int, float))
        else "string" if isinstance(current, str)
        else "list" if isinstance(current, list)
        else "object" if isinstance(current, dict)
        else None
    )
    ok = (
        (expected == "boolean" and isinstance(new, bool))
        or (expected == "number" and isinstance(new, (int, float)) and not isinstance(new, bool))
        or (expected == "string" and isinstance(new, str))
        or (expected == "list" and isinstance(new, list))
        or (expected == "object" and isinstance(new, dict))
        or expected is None
    )
    if not ok:
        raise SettingsError(f"{group}.{key} is a {expected} (currently {current!r}); got {new!r}")


def unknown_key_error(group, key, available):
    close = difflib.get_close_matches(key, list(available), n=3)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return SettingsError(f"{group} has no setting named {key!r}.{hint}")
