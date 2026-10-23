"""
Adapter for GeoDirectory "Advanced Search Filters" (geodir_advance_search_filters): the search fields
configured per post type under <CPT> > Settings > Search.

There is no REST route for these fields. The admin screen prints every field as a hidden settings
form inside <li id="setName_<id>">; clicking a field copies that markup into the settings panel, and
Save posts it with jQuery's serialize() to admin-ajax.php (action geodir_cpt_search_save_field, nonce
#gd_new_field_nonce). This adapter does the same thing with the same markup, so a save here is
byte-for-byte what a person clicking Save would send. It needs a cookie-mode site config.

Groups are post types, e.g. "gd_place". A field is addressed by its id (the number in setName_<id>),
or by its htmlvar_name when that name is used once in the post type. Setting names are the form's
input names (frontend_title, range_min, range_max, range_step, range_from_title, ...).

Behaviour that is easy to get wrong, all handled here (found against a real install):

* A save REPLACES the whole row from the POSTed form, so every input is sent, not just the changed one.
* Range fields render as bands: "Less than <min>", then <min>-<min+step>, ... up to <max>, then
  "More than <max>". When (max - min) is not a whole number of steps, the plugin prints the leftover band
  raw ("95,000-100000" next to formatted "£85,000-£95,000"). A min, max or step of 0 is treated as empty
  and replaced with 10, 50 and 10 ("Less than 10" on a 0-5 field). update() warns about both before writing.
* The form always carries range_mode=1 (a hidden input), whatever is stored; so does a save from the
  admin screen. It only changes the output when range_step is 1 (single values instead of bands).
* Deleting a field cannot be undone. It only happens when the change for that field is exactly
  {"delete": true}.
"""

import re
from html.parser import HTMLParser

from .base import SettingsAdapter, SettingsError, unknown_key_error

NONCE_ID = "gd_new_field_nonce"
SKIP_INPUT_TYPES = ("submit", "button", "image", "reset", "file")
# Inputs that belong to the form plumbing, not to the field's settings.
PLUMBING = ("_wpnonce", "_wp_http_referer", "security", "action")
RANGE_KEYS = ("range_min", "range_max", "range_step")


class _InputsParser(HTMLParser):
    """What jQuery's serialize() sends for the inputs in a chunk of markup: (name, value) pairs in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs = []
        self._select = None       # {"name", "options": [(value, selected)], "multiple"}
        self._option = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if not name or "disabled" in a or kind in SKIP_INPUT_TYPES:
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            value = a.get("value")
            if value is None:
                value = "on" if kind in ("checkbox", "radio") else ""
            self.pairs.append((name, value))
        elif tag == "select" and name and "disabled" not in a:
            self._select = {"name": name, "options": [], "multiple": "multiple" in a}
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "selected": "selected" in a, "text": ""}
        elif tag == "textarea" and name and "disabled" not in a:
            self._textarea = {"name": name, "text": ""}

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data
        if self._textarea is not None:
            self._textarea["text"] += data

    def handle_endtag(self, tag):
        if tag == "option" and self._option is not None:
            o, self._option = self._option, None
            self._select["options"].append((o["value"] if o["value"] is not None else o["text"].strip(), o["selected"]))
        elif tag == "select" and self._select is not None:
            s, self._select = self._select, None
            chosen = [v for v, sel in s["options"] if sel]
            if not chosen and not s["multiple"] and s["options"]:
                chosen = [s["options"][0][0]]        # a browser selects the first option
            self.pairs += [(s["name"], v) for v in chosen]
        elif tag == "textarea" and self._textarea is not None:
            t, self._textarea = self._textarea, None
            self.pairs.append((t["name"], t["text"]))


class _ScreenParser(HTMLParser):
    """Find each <li id="setName_<id>">, the settings template inside it (the first
    <script type="text/template" class="dd-setting ...">, which is what the admin copies into its
    settings panel) and the page's gd_new_field_nonce."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.nonce = None
        self.templates = {}       # id -> template markup
        self._li_stack = []       # field id (or None) per open <li>
        self._capture = None      # field id whose template is being read
        self._buf = []

    def _current(self):
        for fid in reversed(self._li_stack):
            if fid is not None:
                return fid
        return None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            m = re.fullmatch(r"setName_(\d+)", a.get("id") or "")
            self._li_stack.append(m.group(1) if m else None)
        elif tag == "input" and a.get("id") == NONCE_ID:
            self.nonce = a.get("value")
        elif tag == "script" and "dd-setting" in (a.get("class") or "").split():
            fid = self._current()
            if fid is not None and fid not in self.templates:
                self._capture, self._buf = fid, []

    def handle_data(self, data):
        if self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "li" and self._li_stack:
            self._li_stack.pop()
        elif tag == "script" and self._capture is not None:
            self.templates[self._capture] = "".join(self._buf)
            self._capture = None


def parse_screen(html):
    """Return (nonce, {field_id: [(name, value), ...]}) from the CPT search settings screen."""
    screen = _ScreenParser()
    screen.feed(html)
    screen.close()
    fields = {}
    for fid, markup in screen.templates.items():
        inputs = _InputsParser()
        inputs.feed(markup)
        inputs.close()
        fields[fid] = inputs.pairs
    return screen.nonce, fields


def effective(pairs):
    """What PHP sees in $_POST for scalar names: the last value of each name. Plumbing is left out."""
    out = {}
    for name, value in pairs:
        if name in PLUMBING or "[TEMP]" in name:
            continue
        out[name] = value
    return out


def range_warnings(label, values):
    """Problems the plugin's band rendering will show for these range settings. The plugin replaces an
    empty (or 0) range_min with 10, range_max with 50 and range_step with 10 before drawing the bands."""
    if values.get("input_type") != "RANGE" or values.get("htmlvar_name") == "distance":
        return []
    try:
        lo, hi, step = (int(float(values.get(k) or 0)) for k in RANGE_KEYS)
    except ValueError:
        return [f"{label}: range_min/max/step must be whole numbers"]
    out = []
    for key, val, default in (("range_min", lo, 10), ("range_max", hi, 50), ("range_step", step, 10)):
        if val == 0:
            out.append(f"{label}: {key} 0 is treated as empty and becomes {default}")
    lo, hi, step = lo or 10, hi or 50, step or 10
    if hi <= lo:
        out.append(f"{label}: range_max ({hi}) must be above range_min ({lo})")
    elif (hi - lo) % step:
        out.append(f"{label}: ({hi} - {lo}) is not a multiple of {step}, so the last band prints unformatted")
    return out


class GeodirSearchAdapter(SettingsAdapter):
    slug = "geodir-search"
    title = "GeoDirectory Advanced Search Filters (search fields per post type)"
    description = (
        "Groups are GeoDirectory post types (e.g. 'gd_place'). get(group) lists that post type's search "
        "fields by id with their settings (frontend_title, range_min/max/step, range_from_title, "
        "main_search, ...). update({post_type: {field_id_or_htmlvar: {setting: value}}}) saves through the "
        "plugin's own admin-ajax action with the full form, exactly like the admin screen; "
        "{field: {'delete': true}} removes a field (irreversible). Needs a cookie-mode config. "
        "One admin page read per post type, one POST per changed field, one re-read to verify."
    )

    def groups(self):
        return []      # post types differ per site; any 'gd_*' name is accepted

    @staticmethod
    def _validate_group(group):
        if not isinstance(group, str) or not re.fullmatch(r"[a-z0-9_]+", group):
            raise SettingsError(f"Group must be a post type name such as 'gd_place', got {group!r}")

    @staticmethod
    def _read(client, post_type):
        html = client.admin_get("edit.php", {"post_type": post_type, "page": f"{post_type}-settings", "tab": "cpt-search"})
        nonce, fields = parse_screen(html)
        if not nonce:
            raise SettingsError(f"No search settings screen for {post_type!r} (no {NONCE_ID} on the page). "
                                "Is it a GeoDirectory post type, and is Advanced Search Filters active?")
        return nonce, fields

    @staticmethod
    def _resolve(post_type, ref, fields):
        ref = str(ref)
        if ref in fields:
            return ref
        hits = [fid for fid, pairs in fields.items() if effective(pairs).get("htmlvar_name") == ref]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SettingsError(f"{post_type}: {ref!r} is used by fields {', '.join(hits)}; address one by id")
        names = sorted({effective(p).get("htmlvar_name", "") for p in fields.values()})
        raise SettingsError(f"{post_type}: no search field {ref!r}. Fields: {', '.join(names)}")

    def get(self, client, group=None, keys=None):
        if group is None:
            return {
                "plugin": self.slug,
                "groups": "any GeoDirectory post type, e.g. 'gd_place'",
                "hint": "Call again with `group` set to a post type to list its search fields.",
                "requests_sent": 0,
            }
        self._validate_group(group)
        before = client.request_count
        _, fields = self._read(client, group)
        values = {fid: effective(pairs) for fid, pairs in fields.items()}
        if keys:
            values = {self._resolve(group, k, fields): values[self._resolve(group, k, fields)] for k in keys}
        warnings = []
        for fid, v in values.items():
            warnings += range_warnings(f"{group}#{fid} {v.get('htmlvar_name')}", v)
        return {"plugin": self.slug, "group": group, "values": values, "warnings": warnings,
                "requests_sent": client.request_count - before}

    def update(self, client, changes, dry_run=True, **_ignored):
        if not isinstance(changes, dict) or not changes:
            raise SettingsError('`changes` must be an object like {"gd_place": {"<field id>": {"range_max": 100}}}')
        before = client.request_count
        report = {"plugin": self.slug, "dry_run": bool(dry_run), "changes": [], "applied": [],
                  "verified": None, "warnings": []}
        plan = []        # (post_type, nonce, field_id, new_pairs | None for delete, wanted)
        for post_type, per_field in changes.items():
            self._validate_group(post_type)
            if not isinstance(per_field, dict) or not per_field:
                raise SettingsError(f"changes[{post_type!r}] must map a field id or htmlvar_name to settings")
            nonce, fields = self._read(client, post_type)
            for ref, kv in per_field.items():
                fid = self._resolve(post_type, ref, fields)
                current = effective(fields[fid])
                label = f"{post_type}#{fid} {current.get('htmlvar_name')}"
                if kv == {"delete": True}:
                    report["changes"].append({"group": post_type, "field": fid, "htmlvar_name": current.get("htmlvar_name"),
                                              "key": "(field)", "old": current.get("frontend_title"), "new": "DELETED"})
                    plan.append((post_type, nonce, fid, None, None))
                    continue
                if not isinstance(kv, dict) or not kv or "delete" in kv:
                    raise SettingsError(f"{label}: give settings to change, or exactly {{\"delete\": true}}")
                wanted = {}
                for key, new in kv.items():
                    if key in PLUMBING or key not in current:
                        raise unknown_key_error(label, key, [k for k in current if k not in PLUMBING])
                    if isinstance(new, bool) or not isinstance(new, (str, int, float)):
                        raise SettingsError(f"{label}.{key}: values are strings or numbers, got {new!r}")
                    new = str(int(new)) if isinstance(new, float) and new.is_integer() else str(new)
                    if current[key] != new:
                        wanted[key] = new
                        report["changes"].append({"group": post_type, "field": fid, "htmlvar_name": current.get("htmlvar_name"),
                                                  "key": key, "old": current[key], "new": new})
                if not wanted:
                    continue
                report["warnings"] += range_warnings(label, {**current, **wanted})
                # Every occurrence of a changed name takes the new value (hidden + checkbox pairs post twice).
                new_pairs = [(n, wanted.get(n, v)) for n, v in fields[fid]]
                plan.append((post_type, nonce, fid, new_pairs, wanted))

        if not report["changes"]:
            report["message"] = "Nothing to change - the requested values are already set."
        elif dry_run:
            report["message"] = "Dry run - nothing written. Call again with dry_run=false to apply."
        if dry_run or not plan:
            report["requests_sent"] = client.request_count - before
            return report

        problems = []
        for post_type, nonce, fid, new_pairs, _ in plan:
            if new_pairs is None:
                resp = client.admin_ajax([("action", "geodir_cpt_search_delete_field"), ("security", nonce),
                                          ("id", fid), ("post_type", post_type)])
            else:
                resp = client.admin_ajax(list(new_pairs) + [("security", nonce), ("action", "geodir_cpt_search_save_field")])
            if not (isinstance(resp, dict) and resp.get("success")):
                problems.append(f"{post_type}#{fid}: the plugin refused the save: {str(resp)[:200]}")
                break        # never carry on writing after a failure
            report["applied"].append(f"{post_type}#{fid}")

        # Verify from a fresh read of each touched screen, not from the save responses.
        for post_type in dict.fromkeys(p[0] for p in plan):
            _, fields = self._read(client, post_type)
            for pt, _, fid, new_pairs, wanted in plan:
                if pt != post_type or f"{pt}#{fid}" not in report["applied"]:
                    continue
                if new_pairs is None:
                    if fid in fields:
                        problems.append(f"{pt}#{fid}: still present after delete")
                    continue
                stored = effective(fields.get(fid, []))
                for key, new in wanted.items():
                    if stored.get(key) != new:
                        problems.append(f"{pt}#{fid}.{key}: stored {stored.get(key)!r}, wanted {new!r}")

        report["verified"] = not problems
        report["warnings"] += problems
        report["message"] = ("Applied and verified against a fresh read of the settings screen." if not problems
                             else "Stopped or not verified - see warnings.")
        skipped = [f"{p[0]}#{p[2]}" for p in plan if f"{p[0]}#{p[2]}" not in report["applied"]]
        if skipped:
            report["skipped"] = skipped
        report["requests_sent"] = client.request_count - before
        return report
