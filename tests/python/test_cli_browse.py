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
            {"cmd": "browse", "session": "cli", "op": "search",
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


class TestBrowseSessionKey(unittest.TestCase):
    """The session key names a per-consumer browse cursor in the daemon.

    It used to be the literal "widget" for every caller, so `tonearmctl browse
    search x` typed in a terminal moved the BAR's cursor -- and two bar
    surfaces shared one cursor between them. The key is a wire field with real
    isolation behind it (test_core_browse: two keys hold two cursors); these
    tests pin down that the CLI stops claiming to be the widget.
    """

    def test_the_default_key_is_cli_not_widget(self):
        # The whole point: a hand-typed browse must not land on the cursor the
        # bar widget is rendering from.
        self.assertEqual(
            cli.browse_request(["browse", "search", "oingo boingo"])["session"],
            "cli")

    def test_session_flag_sets_the_key(self):
        self.assertEqual(
            cli.browse_request(["browse", "--session", "widget", "back"]),
            {"cmd": "browse", "session": "widget", "op": "back"})

    def test_the_flag_is_not_swallowed_by_the_search_term(self):
        # search joins its remaining argv with spaces, so a flag accepted
        # AFTER the op would silently become part of the term. It belongs
        # before the op, and the term must come through clean.
        request = cli.browse_request(
            ["browse", "--session", "widget", "search", "oingo", "boingo"])
        self.assertEqual(request["session"], "widget")
        self.assertEqual(request["term"], "oingo boingo")

    def test_the_flag_survives_an_index_addressed_op(self):
        request = cli.browse_request(
            ["browse", "--session", "widget", "enter", "2", "7"])
        self.assertEqual(request["session"], "widget")
        self.assertEqual(request["index"], 2)
        self.assertEqual(request["level_id"], 7)

    def test_a_flag_with_no_value_returns_none(self):
        self.assertIsNone(cli.browse_request(["browse", "--session"]))

    def test_an_empty_key_returns_none_rather_than_defaulting(self):
        # The daemon reads `payload.pop("session", None) or "widget"`, so an
        # empty string there falls back to the WIDGET's cursor -- exactly the
        # collision this flag exists to prevent. Refuse it at the edge.
        self.assertIsNone(cli.browse_request(["browse", "--session", "", "back"]))

    def test_the_key_alone_is_not_a_browse_request(self):
        self.assertIsNone(cli.browse_request(["browse", "--session", "cli"]))


if __name__ == "__main__":
    unittest.main()
