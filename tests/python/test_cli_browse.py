import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import cli


class TestBrowseArgv(unittest.TestCase):
    def test_search_builds_the_request(self):
        self.assertEqual(
            cli.browse_request(["browse", "search", "oingo boingo"]),
            {"cmd": "browse", "session": "widget", "op": "search",
             "term": "oingo boingo"})

    def test_search_joins_multiple_words(self):
        # `tonearmctl browse search oingo boingo` without quotes must work.
        self.assertEqual(
            cli.browse_request(["browse", "search", "oingo", "boingo"])["term"],
            "oingo boingo")

    def test_enter_carries_index_and_level_id_as_integers(self):
        request = cli.browse_request(["browse", "enter", "2", "7"])
        self.assertEqual(request["index"], 2)
        self.assertEqual(request["level_id"], 7)

    def test_play_takes_an_index_and_a_level_only(self):
        request = cli.browse_request(["browse", "play", "0", "9"])
        self.assertEqual(request["op"], "play")
        self.assertEqual(request["index"], 0)
        self.assertEqual(request["level_id"], 9)
        # No `action` field at all. Play Now is the only action there is, so
        # the argument that used to select between actions is gone from the
        # wire protocol, not merely defaulted.
        self.assertNotIn("action", request)

    def test_play_rejects_the_old_three_argument_form(self):
        # `browse play 0 queue 9` used to parse. It must not silently succeed
        # now by reading "queue" as the level id.
        self.assertIsNone(cli.browse_request(["browse", "play", "0", "queue", "9"]))

    def test_back_needs_no_arguments(self):
        self.assertEqual(cli.browse_request(["browse", "back"])["op"], "back")

    def test_an_unknown_subcommand_returns_none(self):
        self.assertIsNone(cli.browse_request(["browse", "teleport"]))

    def test_a_non_numeric_index_returns_none_rather_than_raising(self):
        self.assertIsNone(cli.browse_request(["browse", "enter", "x", "7"]))


if __name__ == "__main__":
    unittest.main()
