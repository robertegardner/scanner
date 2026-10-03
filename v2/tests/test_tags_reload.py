"""TSV hot reload + label precedence."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402


class TagFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tags.tsv")
        with open(self.path, "w") as f:
            f.write("# header\n4229\tJackson PD\tuser\n4237\tSheriff / Law Disp (Police)\n")
        self.tf = sa.TagFile(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_and_three_columns(self):
        self.assertEqual(self.tf.get("4229"), ("Jackson PD", "user"))
        self.assertEqual(self.tf.get(4237), ("Sheriff / Law Disp (Police)", "user"))
        self.assertIsNone(self.tf.get("1"))

    def test_reload_on_mtime_change(self):
        self.tf.get("4229")
        with open(self.path, "w") as f:
            f.write("4229\tJPD\trr\n")
        t = time.time() + 5
        os.utime(self.path, (t, t))
        self.assertEqual(self.tf.get("4229"), ("JPD", "rr"))

    def test_missing_file(self):
        self.assertIsNone(sa.TagFile(os.path.join(self.tmp.name, "nope")).get("1"))


class PrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = os.path.join(self.tmp.name, "tags.tsv")
        with open(p, "w") as f:
            f.write("4229\tJackson PD\tuser\n")
        self.orig = sa.TAGS
        sa.TAGS = sa.TagFile(p)
        sa.STATE.cur_tag = "Jackson PD (Police)"       # op25's stale startup tag

    def tearDown(self):
        sa.TAGS = self.orig
        sa.STATE.cur_tag = None
        self.tmp.cleanup()

    def test_tsv_beats_op25_tag(self):
        self.assertEqual(sa.STATE.label_for("4229"), ("Jackson PD", "user"))

    def test_op25_tag_then_raw(self):
        self.assertEqual(sa.STATE.label_for("9999"), ("Jackson PD (Police)", "op25"))
        sa.STATE.cur_tag = None
        self.assertEqual(sa.STATE.label_for("9999"), ("TG 9999", "none"))


class EncryptedFromTest(unittest.TestCase):
    def test_values(self):
        self.assertIsNone(sa._encrypted_from({}))
        self.assertIs(sa._encrypted_from({"encrypted": 0}), False)
        self.assertIs(sa._encrypted_from({"encrypted": 1}), True)
        self.assertIsNone(sa._encrypted_from({"encrypted": "x"}))


if __name__ == "__main__":
    unittest.main()
