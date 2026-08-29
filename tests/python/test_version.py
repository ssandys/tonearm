"""The version Roon shows must not drift from the manifest.

`APPINFO["display_version"]` is the string Roon renders in
Settings -> Extensions. It was a literal, and it drifted for the reason two
copies of one fact always do: the manifest reached 0.9.0 while this stayed at
the 0.1.0 it was written with, because nothing connected them and nothing
could notice. It now reads the manifest; these pin that it keeps doing so.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import core   # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def manifest_version():
    with open(os.path.join(REPO, "manifest.json")) as handle:
        return json.load(handle)["version"]


class TestVersionTracksTheManifest(unittest.TestCase):
    def test_what_roon_shows_is_what_the_manifest_says(self):
        self.assertEqual(core.APPINFO["display_version"], manifest_version())

    def test_the_fallback_is_not_silently_in_use(self):
        # The assertion above passes just as happily if the reader is broken
        # AND the manifest happens to say "0.0.0". More usefully: if the path
        # resolution ever breaks, the first test still passes only by
        # coincidence, while this one fails immediately.
        self.assertNotEqual(core.APPINFO["display_version"], core.VERSION_FALLBACK)

    def test_the_manifest_is_where_the_code_looks_for_it(self):
        # Pins the path arithmetic itself. core.py is two levels below the
        # plugin root; a file moved into or out of tonearm_lib/ breaks this
        # before it can silently fall back in front of a user.
        self.assertTrue(os.path.isfile(core.manifest_path()),
                        "manifest not found at %s" % core.manifest_path())
        self.assertEqual(os.path.realpath(core.manifest_path()),
                         os.path.realpath(os.path.join(REPO, "manifest.json")))


class TestFallback(unittest.TestCase):
    """The fallback is exercised, not merely asserted absent."""

    def test_a_missing_manifest_falls_back(self):
        self.assertEqual(core.plugin_version("/nonexistent/manifest.json"),
                         core.VERSION_FALLBACK)

    def test_malformed_json_falls_back_rather_than_raising(self):
        # A daemon that will not start because it cannot parse its own version
        # is a worse failure than one reporting the wrong number.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(core.plugin_version(path), core.VERSION_FALLBACK)

    def test_a_manifest_with_no_version_key_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"id": "ssandys.tonearm"}')
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(core.plugin_version(path), core.VERSION_FALLBACK)

    def test_an_empty_version_string_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"version": ""}')
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(core.plugin_version(path), core.VERSION_FALLBACK)


if __name__ == "__main__":
    unittest.main()
