"""
geodir-search adapter tests. No network: a fake client serves a cut-down copy of the CPT search settings
screen (fields as <script type="text/template" class="dd-setting"> inside <li id="setName_<id>">) and
plays the plugin's admin-ajax save/delete, which rebuild the row from whatever was POSTed. Like the plugin,
a field save moves the field to the end of the list (no sort_order in the form) and the order save reorders.
"""
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_settings import get_adapter
from plugin_settings.base import SettingsError
from plugin_settings.geodir_search import parse_screen, effective, range_warnings


def field_li(fid, htmlvar, title, input_type="RANGE", lo="5000", hi="100000", step="10000", main=True):
    rng = ""
    if input_type == "RANGE":
        rng = (f'<input type="number" name="range_min" value="{lo}">'
               f'<input type="number" name="range_max" value="{hi}">'
               f'<input type="number" name="range_step" value="{step}">'
               '<input type="hidden" name="range_mode" value="1" />'
               '<select name="search_condition_select"><option value="SINGLE">Single</option>'
               '<option value="FROM">From</option></select>')
    checked = " checked" if main else ""
    return (
        f'<li class="dd-item" data-id="{fid}" id="setName_{fid}">'
        f'<div class="dd-form" onclick="gd_tabs_item_settings(this);">{title}</div>'
        '<script type="text/template" class="dd-setting dd-type-text">'
        f'<input type="hidden" name="_wpnonce" value="n{fid}" />'
        f'<input type="hidden" name="field_id" value="{fid}" />'
        '<input type="hidden" name="post_type" value="gd_place" />'
        f'<input type="hidden" name="htmlvar_name" value="{htmlvar}" />'
        f'<input type="hidden" name="input_type" value="{input_type}" />'
        '<input type="hidden" name="search_condition" value="SELECT" />'
        f'<input type="text" name="frontend_title" value="{title}" />'
        '<input type="hidden" name="main_search" value="0" />'
        f'<input type="checkbox" name="main_search" value="1"{checked} />'
        f'{rng}'
        '<select name="conditional_fields[TEMP][action]"><option value="">-</option></select>'
        '<button type="submit" name="save">Save</button>'
        '</script></li>'
    )


class FakeSite:
    def __init__(self, fields):
        self.rows = {f[0]: f for f in fields}       # id -> args for field_li
        self.request_count = 0
        self.posts = []
        self.refuse = False
        self.order_saves = []

    def screen(self):
        lis = "".join(field_li(*args) for args in self.rows.values())
        return f'<input type="hidden" name="gd_new_field_nonce" id="gd_new_field_nonce" value="NONCE"/><ul>{lis}</ul>'

    def admin_get(self, page, params=None):
        self.request_count += 1
        return self.screen()

    def admin_ajax(self, fields):
        self.request_count += 1
        self.posts.append(list(fields))
        post = {}
        for k, v in fields:
            post[k] = v                               # PHP keeps the last value of a repeated name
        if self.refuse or post.get("security") != "NONCE":
            return {"success": False, "data": "nope"}
        if post["action"] == "geodir_cpt_search_delete_field":
            del self.rows[post["id"]]
            return {"success": True, "data": True}
        if post["action"] == "geodir_cpt_search_save_order":
            ids = [v for k, v in fields if k.endswith("[id]")]
            self.order_saves.append(ids)
            self.rows = {i: self.rows[i] for i in ids}
            return {"success": True, "data": True}
        fid = post["field_id"]
        old = self.rows[fid]
        self.rows[fid] = (fid, post["htmlvar_name"], post["frontend_title"], post["input_type"],
                          post.get("range_min", old[4] if len(old) > 4 else "5000"),
                          post.get("range_max", old[5] if len(old) > 5 else "100000"),
                          post.get("range_step", old[6] if len(old) > 6 else "10000"),
                          post["main_search"] == "1")
        self.rows[fid] = self.rows.pop(fid)      # the plugin gives a saved field MAX(sort_order) + 1
        return {"success": True, "data": "<li>...</li>"}


def site():
    return FakeSite([
        ("1", "price", "Price", "RANGE", "5000", "100000", "10000", True),
        ("2", "gearbox", "Gearbox", "SELECT", "", "", "", False),
        ("3", "gearbox", "Gearbox", "SELECT", "", "", "", False),
        ("4", "engine", "Engine", "RANGE", "0", "5", "1", False),
    ])


class ParseTests(unittest.TestCase):
    def test_reads_templates_like_jquery_serialize(self):
        nonce, fields = parse_screen(site().screen())
        self.assertEqual(nonce, "NONCE")
        self.assertEqual(sorted(fields), ["1", "2", "3", "4"])
        pairs = fields["1"]
        self.assertIn(("main_search", "0"), pairs)
        self.assertIn(("main_search", "1"), pairs)          # checked box follows its hidden twin
        self.assertIn(("search_condition_select", "SINGLE"), pairs)   # nothing selected -> first option
        self.assertNotIn("save", [n for n, _ in pairs])     # buttons are never serialised
        self.assertNotIn(("main_search", "1"), parse_screen(site().screen())[1]["2"])   # unchecked

    def test_effective_is_last_value_without_plumbing(self):
        e = effective(parse_screen(site().screen())[1]["1"])
        self.assertEqual(e["main_search"], "1")
        self.assertNotIn("_wpnonce", e)
        self.assertNotIn("conditional_fields[TEMP][action]", e)

    def test_range_warnings(self):
        e = effective(parse_screen(site().screen())[1]["1"])
        self.assertTrue(any("not a multiple" in w for w in range_warnings("p", e)))
        self.assertEqual(range_warnings("p", {**e, "range_min": "10000"}), [])
        e4 = effective(parse_screen(site().screen())[1]["4"])
        self.assertTrue(any("range_min 0" in w for w in range_warnings("e", e4)))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.site = site()
        self.a = get_adapter("geodir-search")

    def test_dry_run_writes_nothing(self):
        r = self.a.update(self.site, {"gd_place": {"price": {"range_min": 10000}}})
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.site.posts, [])
        self.assertEqual(r["changes"][0]["old"], "5000")
        self.assertEqual(r["changes"][0]["new"], "10000")

    def test_update_posts_full_form_and_verifies(self):
        r = self.a.update(self.site, {"gd_place": {"1": {"range_min": 10000}}}, dry_run=False)
        self.assertTrue(r["verified"], r)
        post = dict(self.site.posts[0])
        self.assertEqual(post["action"], "geodir_cpt_search_save_field")
        self.assertEqual(post["security"], "NONCE")
        self.assertEqual(post["range_min"], "10000")
        self.assertEqual(post["range_max"], "100000")       # untouched settings are sent too
        self.assertEqual(post["frontend_title"], "Price")
        self.assertEqual(self.site.rows["1"][4], "10000")
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["requests_sent"], 4)             # read, save, order, re-read

    def test_field_save_keeps_the_order(self):
        r = self.a.update(self.site, {"gd_place": {"1": {"range_min": 10000}, "4": {"range_min": 1}}}, dry_run=False)
        self.assertTrue(r["verified"], r)
        self.assertEqual(list(self.site.rows), ["1", "2", "3", "4"])
        self.assertEqual(self.site.order_saves, [["1", "2", "3", "4"]])

    def test_delete_then_order_without_the_deleted_field(self):
        r = self.a.update(self.site, {"gd_place": {"3": {"delete": True}, "1": {"range_min": 10000}}}, dry_run=False)
        self.assertTrue(r["verified"], r)
        self.assertEqual(list(self.site.rows), ["1", "2", "4"])

    def test_set_order_on_its_own(self):
        r = self.a.update(self.site, {"gd_place": {"_order": ["4", "1", "2", "3"]}})
        self.assertEqual(r["changes"][0]["key"], "order")
        self.assertEqual(self.site.order_saves, [])           # dry run
        r = self.a.update(self.site, {"gd_place": {"_order": [4, 1, 2, 3]}}, dry_run=False)
        self.assertTrue(r["verified"], r)
        self.assertEqual(list(self.site.rows), ["4", "1", "2", "3"])
        with self.assertRaises(SettingsError):
            self.a.update(self.site, {"gd_place": {"_order": ["4", "1"]}})

    def test_checkbox_pair_both_take_new_value(self):
        self.a.update(self.site, {"gd_place": {"2": {"main_search": "1"}}}, dry_run=False)
        sent = [v for k, v in self.site.posts[0] if k == "main_search"]
        self.assertEqual(sent, ["1"])
        self.assertTrue(self.site.rows["2"][7])

    def test_ambiguous_htmlvar_needs_an_id(self):
        with self.assertRaises(SettingsError):
            self.a.update(self.site, {"gd_place": {"gearbox": {"frontend_title": "X"}}})

    def test_unknown_setting_rejected(self):
        with self.assertRaises(SettingsError):
            self.a.update(self.site, {"gd_place": {"1": {"rnage_min": 1}}})
        with self.assertRaises(SettingsError):
            self.a.update(self.site, {"gd_place": {"1": {"_wpnonce": "x"}}})

    def test_delete_only_when_explicit(self):
        with self.assertRaises(SettingsError):
            self.a.update(self.site, {"gd_place": {"3": {"delete": True, "frontend_title": "x"}}}, dry_run=False)
        r = self.a.update(self.site, {"gd_place": {"3": {"delete": True}}}, dry_run=False)
        self.assertTrue(r["verified"], r)
        self.assertNotIn("3", self.site.rows)
        self.assertEqual(dict(self.site.posts[0])["action"], "geodir_cpt_search_delete_field")

    def test_warns_before_writing_a_ragged_range(self):
        r = self.a.update(self.site, {"gd_place": {"1": {"range_max": 98000}}})
        self.assertTrue(any("not a multiple" in w for w in r["warnings"]))

    def test_stops_after_refusal(self):
        self.site.refuse = True
        r = self.a.update(self.site, {"gd_place": {"1": {"range_min": 10000}, "2": {"frontend_title": "G"}}}, dry_run=False)
        self.assertFalse(r["verified"])
        self.assertEqual(len(self.site.posts), 1)
        self.assertIn("gd_place#1", r["skipped"])

    def test_nothing_to_change(self):
        r = self.a.update(self.site, {"gd_place": {"1": {"range_min": "5000"}}}, dry_run=False)
        self.assertEqual(r["changes"], [])
        self.assertEqual(self.site.posts, [])


if __name__ == "__main__":
    unittest.main()
