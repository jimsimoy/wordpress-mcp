"""
Adapter for "Print Invoice & Delivery Notes for WooCommerce" v7+ (Tyche Softwares).

The plugin's admin screen saves through two REST routes, and the same routes are used here:

    GET/POST  /wcdn/v1/settings    general settings         -> group "settings"
    GET/POST  /wcdn/v1/templates   one object per document  -> groups "templates.invoice",
                                   "templates.receipt", "templates.deliverynote",
                                   "templates.packingslip", "templates.creditnote"

Both need `manage_woocommerce` and the standard `wp_rest` nonce (cookie auth provides both).

Behaviour that is easy to get wrong, all handled here (found by testing against a real install):

* Saves REPLACE, they do not merge. The route rebuilds the stored value from the POSTed data plus the
  plugin's DEFAULTS, not the current values, so posting only the changed keys silently resets every
  other setting. update() therefore reads the full object, changes just the requested keys, and posts
  the whole thing back.
* GET /settings also returns nextInvoiceNumber and startingNumberForEachYear. Posting them back would
  touch invoice numbering, so they are never sent, and this adapter refuses to change them.
* The plugin's 'html' fields (storeAddress, footerText, complimentaryClose, policies) are sanitised with
  str_replace("\\n", "<br />"). A stored "\\r\\n" therefore becomes "\\r<br />", and templates that then
  run nl2br() print every line break twice: an address with blank lines between its lines. Line breaks
  are normalised before those fields are saved; a value that already renders correctly is left alone.
* Every template object carries applyZoomToAll, a one-shot "copy page setup to all templates" box. It is
  always sent as false so a save can never overwrite the other documents' zoom.
* Reading /templates rebuilds the plugin's admin preview from a random order and, when that order has
  no invoice number yet, assigns it the next one - the same thing happens whenever anyone opens the
  plugin's admin screen. It burns one number per read, so templates are only read when a templates.*
  group is actually requested, and once per call.
* The settings save answers with the settings wrapped in a serialised REST response
  ({"data": {"status": "success", "data": {...}}, ...}); unwrap() digs the settings out. Verification
  uses these save responses, so no extra reads are needed.
"""

import copy
import re

from .base import SettingsAdapter, SettingsError, check_type, unknown_key_error

SETTINGS_ROUTE = "/wcdn/v1/settings"
TEMPLATES_ROUTE = "/wcdn/v1/templates"

KNOWN_TEMPLATES = ("invoice", "receipt", "deliverynote", "packingslip", "creditnote")
COUNTER_KEYS = ("nextInvoiceNumber", "startingNumberForEachYear")
HTML_FIELDS = ("storeAddress", "footerText", "complimentaryClose", "policies")

_BR = r"<br\s*/?>"


def clean_breaks(value):
    """Return value with line breaks in a form the plugin's sanitiser, then nl2br(), print once each."""
    value = value.replace("\r\n", "\n")
    value = re.sub(r"\r(?=" + _BR + ")", "", value)      # "\r<br />" left behind by an earlier save
    value = value.replace("\r", "\n")
    value = re.sub("(" + _BR + r")[ \t]*\n", r"\1", value)  # a newline right after a <br> doubles it
    return value


def render_breaks(value):
    """How many line breaks nl2br() prints for value: each <br> tag, plus each \\r\\n / \\r / \\n."""
    return len(re.findall(_BR, value)) + len(re.findall(r"\r\n|\r|\n", value))


def stored_form(value):
    """What the plugin stores for an html field once it has sanitised value."""
    return clean_breaks(value).replace("\n", "<br />")


def line_break_problem(value):
    """True when value would print more line breaks than it has lines."""
    return isinstance(value, str) and render_breaks(value) != render_breaks(stored_form(value))


def unwrap(obj):
    """Dig the settings dict out of the serialised REST response the settings save returns."""
    while isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        obj = obj["data"]
    return obj


def _check_response(resp, what):
    if not isinstance(resp, dict) or resp.get("status") != "success":
        detail = ""
        if isinstance(resp, dict):
            data = resp.get("data")
            detail = (data.get("error_description") if isinstance(data, dict) else None) or resp.get("message") or ""
        raise SettingsError(f"{what} failed: {detail or str(resp)[:200]}")
    return resp


class WcdnAdapter(SettingsAdapter):
    slug = "wcdn"
    title = "Print Invoice & Delivery Notes for WooCommerce (v7+)"
    description = (
        "Settings groups: 'settings' (general: print behaviour, currency code in PDFs, store details, "
        "email attachments, invoice numbering) and 'templates.<name>' for each document "
        "(invoice, receipt, deliverynote, packingslip, creditnote: which fields show, fonts, layout). "
        "Requires the plugin at v7 or later (the REST routes do not exist in v6). Changes are applied "
        "as read-modify-write of the full object; invoice counters are never touched; line breaks in "
        "text fields are normalised so an address does not print with blank lines."
    )

    def groups(self):
        return ["settings"] + [f"templates.{t}" for t in KNOWN_TEMPLATES]

    # ── reading ────────────────────────────────────────────────────────────

    def _read_settings(self, client):
        resp = _check_response(client.rest("GET", SETTINGS_ROUTE), "Reading wcdn settings")
        return resp["data"]

    def _read_templates(self, client):
        resp = _check_response(client.rest("GET", TEMPLATES_ROUTE), "Reading wcdn templates")
        templates = (resp.get("data") or {}).get("templates")
        if not isinstance(templates, dict):
            raise SettingsError("Unexpected response from the templates route (is the plugin v7 or later?)")
        return templates

    @staticmethod
    def _template_name(group):
        return group.split(".", 1)[1] if group.startswith("templates.") else None

    def _validate_group(self, group):
        if group == "settings":
            return
        name = self._template_name(group)
        if not name or not re.fullmatch(r"[a-z0-9_]+", name):
            raise SettingsError(f"Unknown group {group!r}. Use one of: {', '.join(self.groups())}")

    def get(self, client, group=None, keys=None):
        if group is None:
            return {
                "plugin": self.slug,
                "groups": self.groups(),
                "hint": "Call again with `group` to read that group's settings. Reading a templates.* group "
                        "may assign the next invoice number to one order (plugin behaviour).",
                "requests_sent": 0,
            }
        self._validate_group(group)
        before = client.request_count
        warnings = []

        if group == "settings":
            values = self._read_settings(client)
            for field in HTML_FIELDS:
                if line_break_problem(values.get(field)):
                    warnings.append(
                        f"settings.{field} would print with doubled line breaks (a stored \\r\\n or "
                        f"\\r<br />). update(..., fix_line_breaks=true) normalises it."
                    )
        else:
            name = self._template_name(group)
            templates = self._read_templates(client)
            if name not in templates:
                raise SettingsError(f"No template {name!r}. Available: {', '.join(sorted(templates))}")
            values = templates[name]
            warnings.append("Reading templates may have assigned the next invoice number to one order.")

        if keys:
            missing = [k for k in keys if k not in values]
            if missing:
                raise unknown_key_error(group, missing[0], values)
            values = {k: values[k] for k in keys}

        return {
            "plugin": self.slug,
            "group": group,
            "values": values,
            "warnings": warnings,
            "requests_sent": client.request_count - before,
        }

    # ── writing ────────────────────────────────────────────────────────────

    def update(self, client, changes, dry_run=True, fix_line_breaks=False, **_ignored):
        before = client.request_count
        changes = self._validate_changes(changes)

        settings = None
        templates = None
        if "settings" in changes or fix_line_breaks:
            settings = self._read_settings(client)
            changes.setdefault("settings", {})
        wanted_templates = [g for g in changes if g != "settings"]
        if wanted_templates:
            templates = self._read_templates(client)

        # Work out exactly what would change, validating every key against what is really there.
        diffs = []
        current_by_group = {}
        for group, kv in changes.items():
            if group == "settings":
                current = settings
            else:
                name = self._template_name(group)
                if name not in templates:
                    raise SettingsError(f"No template {name!r}. Available: {', '.join(sorted(templates))}")
                current = templates[name]
            current_by_group[group] = current
            for key, new in kv.items():
                if key not in current:
                    raise unknown_key_error(group, key, current)
                check_type(group, key, current[key], new)

        # Text fields are normalised whenever settings are going to be saved (they all pass through the
        # plugin's sanitiser on save, so leaving a stored \r\n would turn it into \r<br />), and the plan
        # says so. fix_line_breaks also does it when nothing else in settings changes.
        html_changes = {}
        if settings is not None:
            will_save = any(settings.get(k) != v for k, v in changes["settings"].items())
            if will_save or fix_line_breaks:
                for field in HTML_FIELDS:
                    value = changes["settings"].get(field, settings.get(field))
                    if isinstance(value, str) and clean_breaks(value) != value:
                        html_changes[field] = clean_breaks(value)
                changes["settings"].update(html_changes)

        for group, kv in changes.items():
            current = current_by_group[group]
            for key, new in kv.items():
                if current.get(key) != new:
                    entry = {"group": group, "key": key, "old": current.get(key), "new": new}
                    if key in html_changes:
                        entry["note"] = "line breaks normalised so each prints once"
                    diffs.append(entry)

        report = {
            "plugin": self.slug,
            "dry_run": bool(dry_run),
            "changes": diffs,
            "applied": [],
            "verified": None,
            "warnings": [],
        }
        if not diffs:
            report["message"] = "Nothing to change - the requested values are already set."
            report["requests_sent"] = client.request_count - before
            return report
        if dry_run:
            report["message"] = "Dry run - nothing written. Call again with dry_run=false to apply."
            report["requests_sent"] = client.request_count - before
            return report

        problems = []
        groups_with_changes = []
        for d in diffs:
            if d["group"] not in groups_with_changes:
                groups_with_changes.append(d["group"])

        for group in groups_with_changes:
            wanted = changes[group]
            if group == "settings":
                body = {k: v for k, v in settings.items() if k not in COUNTER_KEYS}
                body.update(wanted)      # already includes the line-break-cleaned html fields (see above)
                resp = _check_response(client.rest("POST", SETTINGS_ROUTE, body), "Saving wcdn settings")
                saved = unwrap(resp["data"].get("settings"))
                problems += self._verify(group, wanted, saved)
            else:
                name = self._template_name(group)
                data = copy.deepcopy(templates[name])
                data.update(wanted)
                data["applyZoomToAll"] = False
                resp = _check_response(
                    client.rest("POST", TEMPLATES_ROUTE, {"template": name, "data": data}),
                    f"Saving wcdn template {name}",
                )
                saved = resp["data"].get("template")
                problems += self._verify(group, wanted, saved)
            report["applied"].append(group)
            if problems:
                break        # never carry on writing after a failed verification

        report["verified"] = not problems
        if not problems:
            report["message"] = "Applied. Every changed value was checked against the plugin's own save response."
        if problems:
            report["warnings"] += problems
            skipped = [g for g in groups_with_changes if g not in report["applied"]]
            if skipped:
                report["skipped"] = skipped
        report["requests_sent"] = client.request_count - before
        return report

    # ── helpers ────────────────────────────────────────────────────────────

    def _validate_changes(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("`changes` must be an object like {\"settings\": {\"key\": value}}")
        out = {}
        for group, kv in changes.items():
            self._validate_group(group)
            if not isinstance(kv, dict) or not kv:
                raise SettingsError(f"changes[{group!r}] must be a non-empty object of key: value")
            for key in kv:
                if key in COUNTER_KEYS:
                    raise SettingsError(
                        f"{key} is an invoice counter and is never changed through this tool - "
                        "set it in the plugin's admin screen if it really must change."
                    )
            out[group] = dict(kv)
        return out

    @staticmethod
    def _verify(group, wanted, saved):
        """Compare what the plugin says it stored with what was asked for."""
        if not isinstance(saved, dict):
            return [f"{group}: the save response did not include the stored values, so it could not be verified"]
        problems = []
        for key, new in wanted.items():
            got = saved.get(key)
            if key in HTML_FIELDS and isinstance(new, str) and isinstance(got, str):
                if render_breaks(got) != render_breaks(stored_form(new)) or "\r" in got:
                    problems.append(f"{group}.{key}: line breaks not stored as expected ({new!r} -> {got!r})")
            elif got != new:
                problems.append(f"{group}.{key}: stored {got!r}, wanted {new!r}")
        return problems
