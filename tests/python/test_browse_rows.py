import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse


class TestStripMarkup(unittest.TestCase):
    def test_strips_a_roon_link(self):
        # Measured from yavin: album subtitles arrive as link markup.
        self.assertEqual(
            browse.strip_markup("[[827514|Oingo Boingo]]"), "Oingo Boingo")

    def test_strips_several_links_and_keeps_the_text_between(self):
        self.assertEqual(
            browse.strip_markup("[[1|Danny Elfman]], [[2|Oingo Boingo]]"),
            "Danny Elfman, Oingo Boingo")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(browse.strip_markup("3 Results"), "3 Results")

    def test_none_becomes_empty_string_not_none(self):
        # spec 5.3: subtitle is never null.
        self.assertEqual(browse.strip_markup(None), "")


class TestCapabilities(unittest.TestCase):
    def test_action_list_is_playable_and_not_descendable(self):
        # Measured: track rows in a search result carry hint action_list.
        self.assertEqual(
            browse.capabilities_from_hint({"hint": "action_list"}),
            (False, True))

    def test_list_is_both_because_an_album_is_both(self):
        # spec 2.4: an album is descendable AND playable, and a category is
        # indistinguishable from it here. can_play is optimistic.
        self.assertEqual(
            browse.capabilities_from_hint({"hint": "list"}), (True, True))

    def test_an_item_with_no_hint_is_neither(self):
        # The "No Results" sentinel arrives with hint absent.
        self.assertEqual(browse.capabilities_from_hint({}), (False, False))

    def test_image_key_never_affects_playability(self):
        # spec 2.4: category rows have image_key None and albums may too.
        # Using it as a proxy misclassifies art-less albums.
        with_art = browse.capabilities_from_hint(
            {"hint": "list", "image_key": "abc"})
        without = browse.capabilities_from_hint(
            {"hint": "list", "image_key": None})
        self.assertEqual(with_art, without)


class TestRowFromItem(unittest.TestCase):
    def setUp(self):
        # Verbatim from the probe output recorded in spec 2.6.
        self.album = {
            "title": "Dead Man's Party",
            "subtitle": "[[827514|Oingo Boingo]]",
            "image_key": "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
            "item_key": "65:0",
            "hint": "list",
        }

    def test_shapes_the_row_the_widget_renders(self):
        row = browse.row_from_item(self.album)
        self.assertEqual(row["title"], "Dead Man's Party")
        self.assertEqual(row["subtitle"], "Oingo Boingo")
        self.assertEqual(row["image_key"], "48f5b5fe1ee1dcd0f89bf0f6babcc93a")
        self.assertTrue(row["can_descend"])
        self.assertTrue(row["can_play"])

    def test_never_leaks_item_key(self):
        # spec 5.3 invariant. This is the guard that makes the stale-key
        # trap unreachable from the widget.
        self.assertNotIn("item_key", browse.row_from_item(self.album))

    def test_a_missing_title_becomes_empty_string(self):
        self.assertEqual(browse.row_from_item({"hint": "list"})["title"], "")


class TestNormalizeRows(unittest.TestCase):
    def test_no_results_sentinel_becomes_an_empty_list(self):
        # spec 2.7: Roon returns count 1 and one item titled "No Results".
        # Passed through, the user could arrow onto it and try to play it.
        self.assertEqual(
            browse.normalize_rows([{"title": "No Results"}]), [])

    def test_a_real_row_named_no_results_is_kept_if_it_has_a_key(self):
        # Only the keyless sentinel is dropped. A genuine library item that
        # happens to be titled "No Results" has an item_key and survives.
        rows = browse.normalize_rows(
            [{"title": "No Results", "item_key": "9:0", "hint": "list"}])
        self.assertEqual(len(rows), 1)

    def test_ordinary_rows_pass_through_in_order(self):
        rows = browse.normalize_rows([
            {"title": "A", "item_key": "1:0", "hint": "list"},
            {"title": "B", "item_key": "1:1", "hint": "list"},
        ])
        self.assertEqual([r["title"] for r in rows], ["A", "B"])
