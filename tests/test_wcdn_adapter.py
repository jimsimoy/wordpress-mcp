"""
wcdn adapter tests. No network: a fake client stands in for the site and reproduces the plugin
behaviours the adapter exists to handle -

  * a save rebuilds the stored value from the POSTed data plus DEFAULTS (a partial POST resets the rest)
  * the 'html' sanitiser is str_replace("\\n", "<br />")
  * reading /templates burns an invoice number
  * the settings save wraps its answer in a serialised REST response
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_settings import get_adapter, adapter_slugs
from plugin_settings.base import SettingsError
from plugin_settings.wcdn import (
    WcdnAdapter, clean_breaks, render_breaks, stored_form, line_break_problem, unwrap,
)

SETTINGS_DEFAULTS = {
    "autoPrintDialog": False, "pdfUseCurrencyCode": True, "storeName": "Store", "storeAddress": "",
    "footerText": "", "complimentaryClose": "", "policies": "", "printEndpoint": "print-order",
    "enablePDF": True,
}
HTML = ("storeAddress", "footerText", "complimentaryClose", "policies")
TEMPLATE_DEFAULTS = {
    "enabled": True, "showDocumentTitle": True, "showShopEmail": True, "showInvoiceNumber": True,
    "documentZoom": 100, "applyZoomToAll": False,
}


class FakeSite:
    """Just enough of the site and plugin for the adapter: `rest()` plus request_count."""

    def __init__(self, settings=None, templates=None):
        self.settings = {**SETTINGS_DEFAULTS, **(settings or {})}
        self.templates = {n: {**TEMPLATE_DEFAULTS, **(templates or {}).get(n, {})}
                          for n in ("invoice", "receipt", "deliverynote", "packingslip", "creditnote")}
        self.request_count = 0
        self.calls = []
        self.invoice_numbers_burned = 0
        self.fail_posts_with = None        # e.g. an error response to return for every POST
        self.ignore_keys = ()              # keys the "plugin" silently refuses to store

    def rest(self, method, path, payload=None, params=None):
        self.request_count += 1
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "POST" and self.fail_posts_with is not None:
            return self.fail_posts_with

        if path == "/wcdn/v1/settings" and method == "GET":
            return {"status": "success",
                    "data": {**self.settings, "nextInvoiceNumber": 7, "startingNumberForEachYear": 1,
                             "meta": {"maxInvoiceNumber": 7}}}
        if path == "/wcdn/v1/settings" and method == "POST":
            stored = {}
            for key, default in SETTINGS_DEFAULTS.items():
                value = payload.get(key, default)
                if key in self.ignore_keys:
                    value = self.settings.get(key, default)
                if key in HTML and isinstance(value, str):
                    value = value.replace("\n", "<br />")
                stored[key] = value
            self.settings = stored
            return {"status": "success", "data": {
                "message": "Settings saved successfully.",
                "settings": {"data": {"status": "success", "data": {**stored, "nextInvoiceNumber": 7}},
                             "headers": [], "status": 200}}}
        if path == "/wcdn/v1/templates" and method == "GET":
            self.invoice_numbers_burned += 1
            return {"status": "success", "data": {"templates": copy.deepcopy(self.templates), "config": {}}}
        if path == "/wcdn/v1/templates" and method == "POST":
            name, data = payload["template"], payload["data"]
            stored = {k: data.get(k, d) for k, d in TEMPLATE_DEFAULTS.items()}
            for key in self.ignore_keys:
                stored[key] = self.templates[name][key]
            self.templates[name] = stored
            return {"status": "success", "data": {"message": "Template saved successfully.", "template": stored}}
        raise AssertionError(f"unexpected call {method} {path}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class LineBreakHelperTests(unittest.TestCase):
    # stored form -> (needs fixing, breaks printed by the template today, breaks after the plugin saves it)
    CASES = {
        "crlf (legacy/migrated)":  ("1 Example Rd\r\nExampleton\r\nEX1 2AB", True, 2, 2),
        "mangled CR + br":         ("1 Example Rd\r<br />Exampleton\r<br />EX1 2AB", True, 4, 2),
        "clean br":                ("1 Example Rd<br />Exampleton<br />EX1 2AB", False, 2, 2),
        "lf only":                 ("1 Example Rd\nExampleton\nEX1 2AB", False, 2, 2),
        "br then newline":         ("1 Example Rd<br />\nExampleton<br />\nEX1 2AB", True, 4, 2),
    }

    def test_each_form_ends_up_printing_one_break_per_line(self):
        for name, (value, needs_fix, printed_now, printed_after) in self.CASES.items():
            with self.subTest(form=name):
                self.assertEqual(clean_breaks(value) != value, needs_fix)
                self.assertEqual(render_breaks(value), printed_now)
                after = stored_form(value)
                self.assertEqual(render_breaks(after), printed_after)
                self.assertNotIn("\r", after)

    def test_only_doubling_forms_are_flagged_as_problems(self):
        self.assertTrue(line_break_problem("a\r<br />b"))
        self.assertFalse(line_break_problem("a\r\nb"))       # ugly but prints once
        self.assertFalse(line_break_problem("a<br />b"))
        self.assertFalse(line_break_problem(None))

    def test_unwrap_digs_settings_out_of_the_nested_response(self):
        self.assertEqual(unwrap({"data": {"status": "success", "data": {"a": 1}}, "status": 200}), {"a": 1})
        self.assertEqual(unwrap({"a": 1}), {"a": 1})


class RegistryTests(unittest.TestCase):
    def test_wcdn_registered_and_unknown_plugin_rejected(self):
        self.assertIn("wcdn", adapter_slugs())
        self.assertIsInstance(get_adapter("wcdn"), WcdnAdapter)
        with self.assertRaises(SettingsError):
            get_adapter("nope")


class GetTests(unittest.TestCase):
    def test_no_group_lists_groups_and_sends_no_requests(self):
        site = FakeSite()
        out = WcdnAdapter().get(site)
        self.assertEqual(site.request_count, 0)
        self.assertIn("settings", out["groups"])
        self.assertIn("templates.receipt", out["groups"])

    def test_get_settings_filters_keys_and_warns_about_doubled_breaks(self):
        site = FakeSite(settings={"storeAddress": "a\r<br />b"})
        out = WcdnAdapter().get(site, "settings")
        self.assertEqual(out["requests_sent"], 1)
        self.assertTrue(any("storeAddress" in w for w in out["warnings"]))
        only = WcdnAdapter().get(site, "settings", keys=["autoPrintDialog"])
        self.assertEqual(only["values"], {"autoPrintDialog": False})

    def test_get_template_reads_templates_once_and_mentions_the_side_effect(self):
        site = FakeSite()
        out = WcdnAdapter().get(site, "templates.receipt")
        self.assertEqual(site.invoice_numbers_burned, 1)
        self.assertEqual(out["values"]["enabled"], True)
        self.assertTrue(any("invoice number" in w for w in out["warnings"]))

    def test_bad_group_and_bad_keys(self):
        with self.assertRaises(SettingsError):
            WcdnAdapter().get(FakeSite(), "templates.")
        with self.assertRaises(SettingsError):
            WcdnAdapter().get(FakeSite(), "templates.nope")
        with self.assertRaises(SettingsError):
            WcdnAdapter().get(FakeSite(), "settings", keys=["doesNotExist"])


class UpdateValidationTests(unittest.TestCase):
    def test_unknown_key_is_rejected_with_a_suggestion_and_nothing_is_written(self):
        site = FakeSite()
        with self.assertRaises(SettingsError) as ctx:
            WcdnAdapter().update(site, {"settings": {"autoPrintDialogg": True}}, dry_run=False)
        self.assertIn("autoPrintDialog", str(ctx.exception))
        self.assertEqual(site.posts(), [])

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(SettingsError):
            WcdnAdapter().update(FakeSite(), {"settings": {"autoPrintDialog": "yes"}}, dry_run=False)
        with self.assertRaises(SettingsError):
            WcdnAdapter().update(FakeSite(), {"templates.receipt": {"documentZoom": True}}, dry_run=False)

    def test_invoice_counters_are_refused(self):
        site = FakeSite()
        with self.assertRaises(SettingsError):
            WcdnAdapter().update(site, {"settings": {"nextInvoiceNumber": 1}}, dry_run=False)
        self.assertEqual(site.request_count, 0)

    def test_bad_shapes_and_groups(self):
        for bad in (None, {}, {"settings": {}}, {"settings": "x"}, {"bogus": {"a": 1}}, {"templates.": {"a": 1}}):
            with self.subTest(bad=bad):
                with self.assertRaises(SettingsError):
                    WcdnAdapter().update(FakeSite(), bad)


class UpdateSettingsTests(unittest.TestCase):
    def test_dry_run_is_the_default_writes_nothing_and_reads_only_settings(self):
        site = FakeSite()
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}})
        self.assertTrue(out["dry_run"])
        self.assertEqual(site.posts(), [])
        self.assertEqual(site.invoice_numbers_burned, 0)          # templates were never read
        self.assertEqual(site.request_count, 1)
        self.assertEqual(out["changes"], [{"group": "settings", "key": "autoPrintDialog", "old": False, "new": True}])
        self.assertEqual(site.settings["autoPrintDialog"], False)

    def test_apply_changes_only_the_requested_keys(self):
        site = FakeSite(settings={"storeName": "Acme Ltd", "footerText": "Phone 123", "policies": "Be nice"})
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True, "pdfUseCurrencyCode": False}},
                                   dry_run=False)
        self.assertTrue(out["verified"], out)
        self.assertEqual(out["applied"], ["settings"])
        self.assertTrue(site.settings["autoPrintDialog"])
        self.assertFalse(site.settings["pdfUseCurrencyCode"])
        # A partial POST would have reset these to the plugin defaults.
        self.assertEqual(site.settings["storeName"], "Acme Ltd")
        self.assertEqual(site.settings["footerText"], "Phone 123")
        self.assertEqual(site.settings["policies"], "Be nice")

    def test_invoice_counters_are_never_posted_back(self):
        site = FakeSite()
        WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}}, dry_run=False)
        body = site.posts()[0][2]
        self.assertNotIn("nextInvoiceNumber", body)
        self.assertNotIn("startingNumberForEachYear", body)

    def test_no_op_writes_nothing(self):
        site = FakeSite(settings={"autoPrintDialog": True})
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}}, dry_run=False)
        self.assertEqual(out["changes"], [])
        self.assertEqual(site.posts(), [])
        self.assertEqual(out["requests_sent"], 1)

    def test_saving_settings_normalises_a_crlf_address_and_says_so(self):
        site = FakeSite(settings={"storeAddress": "1 Example Rd\r\nExampleton\r\nEX1 2AB"})
        plan = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}})
        notes = [c for c in plan["changes"] if c["key"] == "storeAddress"]
        self.assertEqual(len(notes), 1)
        self.assertIn("line breaks", notes[0]["note"])
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}}, dry_run=False)
        self.assertTrue(out["verified"], out)
        self.assertEqual(site.settings["storeAddress"], "1 Example Rd<br />Exampleton<br />EX1 2AB")
        self.assertEqual(render_breaks(site.settings["storeAddress"]), 2)

    def test_without_the_normalising_the_naive_save_would_double_the_breaks(self):
        # Documents why the adapter exists: what the fake plugin does to a stored \r\n if posted as-is.
        site = FakeSite()
        site.rest("POST", "/wcdn/v1/settings", {"storeAddress": "a\r\nb\r\nc"})
        self.assertEqual(site.settings["storeAddress"], "a\r<br />b\r<br />c")
        self.assertEqual(render_breaks(site.settings["storeAddress"]), 4)        # 2 lines, 4 breaks

    def test_explicit_html_change_is_cleaned_before_saving(self):
        site = FakeSite()
        out = WcdnAdapter().update(site, {"settings": {"storeAddress": "1 Road\r\nTown"}}, dry_run=False)
        self.assertTrue(out["verified"], out)
        self.assertEqual(site.settings["storeAddress"], "1 Road<br />Town")

    def test_fix_line_breaks_repairs_a_mangled_address_when_nothing_else_changes(self):
        site = FakeSite(settings={"storeAddress": "a\r<br />b\r<br />c"})
        untouched = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": False}}, dry_run=False)
        self.assertEqual(site.posts(), [])                       # no effective change -> not touched
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": False}}, dry_run=False,
                                   fix_line_breaks=True)
        self.assertTrue(out["verified"], out)
        self.assertEqual(site.settings["storeAddress"], "a<br />b<br />c")
        self.assertEqual(untouched["changes"], [])

    def test_fix_line_breaks_alone_is_enough(self):
        site = FakeSite(settings={"storeAddress": "a\r<br />b"})
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True}}, dry_run=False, fix_line_breaks=True)
        self.assertTrue(out["verified"], out)
        self.assertEqual(site.settings["storeAddress"], "a<br />b")


class UpdateTemplatesTests(unittest.TestCase):
    def test_template_update_sends_the_full_object_with_apply_zoom_off(self):
        site = FakeSite(templates={"receipt": {"documentZoom": 90, "applyZoomToAll": True}})
        out = WcdnAdapter().update(site, {"templates.receipt": {"showDocumentTitle": False}}, dry_run=False)
        self.assertTrue(out["verified"], out)
        method, path, body = site.posts()[0]
        self.assertEqual(body["template"], "receipt")
        self.assertIs(body["data"]["applyZoomToAll"], False)
        self.assertEqual(body["data"]["documentZoom"], 90)        # untouched key carried through
        self.assertFalse(site.templates["receipt"]["showDocumentTitle"])
        self.assertTrue(site.templates["receipt"]["showShopEmail"])

    def test_only_touched_templates_are_saved_and_templates_are_read_once(self):
        site = FakeSite()
        WcdnAdapter().update(site, {"templates.receipt": {"showDocumentTitle": False},
                                    "templates.packingslip": {"enabled": False}}, dry_run=False)
        self.assertEqual(site.invoice_numbers_burned, 1)
        self.assertEqual([c[2]["template"] for c in site.posts()], ["receipt", "packingslip"])
        self.assertTrue(site.templates["receipt"]["enabled"])     # a document we did not touch keeps its values

    def test_a_template_already_at_the_wanted_values_is_not_saved(self):
        site = FakeSite(templates={"receipt": {"showDocumentTitle": False}})
        out = WcdnAdapter().update(site, {"templates.receipt": {"showDocumentTitle": False},
                                          "templates.packingslip": {"enabled": False}}, dry_run=False)
        self.assertEqual([c[2]["template"] for c in site.posts()], ["packingslip"])
        self.assertEqual(out["applied"], ["templates.packingslip"])

    def test_settings_and_templates_together(self):
        site = FakeSite()
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True},
                                          "templates.receipt": {"showShopEmail": False}}, dry_run=False)
        self.assertEqual(out["applied"], ["settings", "templates.receipt"])
        self.assertTrue(out["verified"], out)
        # login is not counted here: 1 read settings + 1 read templates + 2 saves
        self.assertEqual(out["requests_sent"], 4)


class FailureTests(unittest.TestCase):
    def test_a_rejected_save_raises_with_the_plugins_message_and_stops(self):
        site = FakeSite()
        site.fail_posts_with = {"status": "error", "data": {"error_description": "Authentication has failed."}}
        with self.assertRaises(SettingsError) as ctx:
            WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True},
                                        "templates.receipt": {"showShopEmail": False}}, dry_run=False)
        self.assertIn("Authentication has failed", str(ctx.exception))
        self.assertEqual(len(site.posts()), 1)                    # no second write after the failure

    def test_a_value_the_plugin_did_not_store_is_reported_and_stops_further_writes(self):
        site = FakeSite()
        site.ignore_keys = ("autoPrintDialog",)
        out = WcdnAdapter().update(site, {"settings": {"autoPrintDialog": True},
                                          "templates.receipt": {"showShopEmail": False}}, dry_run=False)
        self.assertFalse(out["verified"])
        self.assertTrue(any("autoPrintDialog" in w for w in out["warnings"]))
        self.assertEqual(out["skipped"], ["templates.receipt"])
        self.assertEqual(len(site.posts()), 1)


if __name__ == "__main__":
    unittest.main()
