"""ems.rg2.io P25 panel: Live | Filtered switch, call-by-call player, filter
drawer. The pure FQ helpers are run under node (skipped if node is absent)."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402

HTML = sa.CAPTIONS_HTML


def run_js(expr):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    m = re.search(r"/\*FQ-BEGIN\*/(.*?)/\*FQ-END\*/", HTML, re.S)
    src = m.group(1) + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=10)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class MarkupTest(unittest.TestCase):
    def test_elements_and_endpoints_present(self):
        for s in ('id="src-live"', 'id="src-filt"', 'id="fpanel"', 'id="faudio"',
                  'id="fplay"', 'id="fnow"', 'id="fstat"', 'id="fbtn"', 'id="fdrawer"',
                  'id="flist"', 'id="fnote"', 'id="faddtg"', 'id="faddbtn"',
                  "'/api/archive/live", "'/api/archive/filter'", "'/api/whoami'",
                  "/*FQ-BEGIN*/", "/*FQ-END*/"):
            self.assertIn(s, HTML, s)

    def test_filtered_audio_joins_pause_others(self):
        self.assertIn("['noaaaudio','p25audio','faudio','audio']", HTML)

    def test_clip_error_skips_to_next(self):
        self.assertRegex(HTML, r"faudio'\)\.addEventListener\('error',function\(\)\{[^}]*fNext\(\)")

    def test_filter_saved_with_post_json(self):
        self.assertIn("fetch('/api/archive/filter',{method:'POST',headers:{'Content-Type':'application/json'}", HTML)


class FqTest(unittest.TestCase):
    def test_allows(self):
        self.assertEqual(run_js(
            "[FQ.allows({default:'play',tg:{'5':'mute'}},5),FQ.allows({default:'play',tg:{'5':'mute'}},6),"
            "FQ.allows({default:'mute',tg:{'5':'play'}},5),FQ.allows({default:'mute',tg:{}},null),"
            "FQ.allows(null,5)]"), [False, True, True, False, True])

    def test_label(self):
        self.assertEqual(run_js(
            "[FQ.label({default:'mute',tg:{'1':'play','2':'mute'}}),FQ.label({default:'play',tg:{'1':'mute'}}),"
            "FQ.label({default:'play',tg:{}}),FQ.label(null)]"),
            ["Filter · only 1", "Filter · 1 muted", "Filter · all", "Filter"])

    def test_merge_dedupes_and_keeps_order(self):
        self.assertEqual(run_js(
            "(function(){var s={};var a=FQ.merge([],[{id:1,start_ts:100},{id:2,start_ts:101}],s,105,120);"
            "var b=FQ.merge(a.queue,[{id:2,start_ts:101},{id:3,start_ts:102}],s,106,120);"
            "return [b.queue.map(function(c){return c.id}),b.skipped]})()"), [[1, 2, 3], 0])

    def test_merge_skips_stale_backlog_to_newest(self):
        self.assertEqual(run_js(
            "(function(){var m=FQ.merge([],[{id:1,start_ts:0},{id:2,start_ts:50},{id:3,start_ts:290}],{},300,120);"
            "return [m.queue.map(function(c){return c.id}),m.skipped]})()"), [[3], 2])

    def test_groups_order_and_unknown_to_other(self):
        self.assertEqual(run_js(
            "FQ.groups([{tgid:1,group:'other'},{tgid:2,group:'fire'},{tgid:3,group:'weird'},{tgid:4,group:'fire'}])"
            ".map(function(g){return [g.group,g.rows.map(function(r){return r.tgid})]})"),
            [["fire", [2, 4]], ["other", [1, 3]]])

    def test_set_group_does_not_mutate(self):
        self.assertEqual(run_js(
            "(function(){var f={default:'play',tg:{'1':'play'}};var n=FQ.setGroup(f,[{tgid:1},{tgid:2}],'mute');"
            "return [f.tg,n.tg,n.default]})()"),
            [{"1": "play"}, {"1": "mute", "2": "mute"}, "play"])


if __name__ == "__main__":
    unittest.main()
