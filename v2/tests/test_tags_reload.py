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


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tags.tsv")
        with open(self.path, "wb") as f:
            f.write(b"4229\tJackson PD\tuser\n")
        self.orig = sa.TAGS
        sa.TAGS = sa.TagFile(self.path)
        sa.STATE.cur_tag = "Op25 Tag"

    def tearDown(self):
        sa.TAGS = self.orig
        sa.STATE.cur_tag = None
        sa.STATE.open_call = None
        sa.STATE.cur_encrypted = None
        self.tmp.cleanup()

    def test_non_utf8_does_not_raise(self):
        with open(self.path, "wb") as f:
            f.write(b"4229\tJackson \xff\xfe PD\tuser\n")
        t = time.time() + 5
        os.utime(self.path, (t, t))
        lab, src = sa.STATE.label_for("4229")
        self.assertIn("Jackson", lab)

    def test_label_for_never_raises(self):
        class Boom:
            def get(self, _):
                raise ValueError("bad")
        sa.TAGS = Boom()
        self.assertEqual(sa.STATE.label_for("1"), ("Op25 Tag", "op25"))
        sa.STATE.on_tgid("1")
        self.assertEqual(sa.STATE.open_call["tgid"], "1")

    def test_reload_on_inode_change_same_mtime(self):
        self.assertEqual(sa.TAGS.get("4229"), ("Jackson PD", "user"))
        st = os.stat(self.path)
        new = self.path + ".new"
        with open(new, "w") as f:
            f.write("4229\tOther\trr\n")
        os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(new, self.path)
        self.assertEqual(sa.TAGS.get("4229"), ("Other", "rr"))

    def test_encrypted_semantics(self):
        S = sa.STATE
        S.cur_encrypted = True                     # previous call's flag
        S.on_tgid("4229")
        self.assertIsNone(S.open_call["encrypted"])
        S.cur_encrypted = False
        S.on_tgid("4229")
        self.assertIs(S.open_call["encrypted"], False)
        S.cur_encrypted = True
        S.on_tgid("4229")
        self.assertIs(S.open_call["encrypted"], True)
        S.cur_encrypted = False
        S.on_tgid("4229")
        self.assertIs(S.open_call["encrypted"], True)   # sticky


class EncryptedFromTest(unittest.TestCase):
    def test_values(self):
        self.assertIsNone(sa._encrypted_from({}))
        self.assertIs(sa._encrypted_from({"encrypted": 0}), False)
        self.assertIs(sa._encrypted_from({"encrypted": 1}), True)
        self.assertIsNone(sa._encrypted_from({"encrypted": "x"}))


if __name__ == "__main__":
    unittest.main()
