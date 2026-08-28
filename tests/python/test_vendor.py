"""The vendored roonapi must import under the system interpreter with no venv."""
import inspect
import os
import sys
import unittest

VENDOR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "vendor")
sys.path.insert(0, os.path.abspath(VENDOR))


class TestVendoredRoonApi(unittest.TestCase):
    def test_imports(self):
        import roonapi
        self.assertTrue(hasattr(roonapi, "RoonApi"))
        self.assertTrue(hasattr(roonapi, "RoonDiscovery"))

    def test_constructor_signature_is_what_we_depend_on(self):
        # A refresh that changes this breaks scripts/tonearm_lib/core.py.
        import roonapi
        params = list(inspect.signature(roonapi.RoonApi.__init__).parameters)
        self.assertEqual(params[:5], ["self", "appinfo", "token", "host", "port"])

    def test_image_url_is_plain_http_on_the_core(self):
        # The whole art strategy rests on this being an unauthenticated URL.
        import roonapi
        src = inspect.getsource(roonapi.RoonApi.get_image)
        self.assertIn("http://%s:%s/api/image/", src)

    def test_only_third_party_import_is_websocket(self):
        # Guards the "dependencies are over-declared" finding across refreshes.
        import roonapi, pathlib, re
        pkg = pathlib.Path(inspect.getfile(roonapi)).parent
        found = set()
        for path in pkg.glob("*.py"):
            for line in path.read_text().splitlines():
                m = re.match(r"\s*(?:import|from)\s+([a-zA-Z_][\w]*)", line)
                if m:
                    found.add(m.group(1))
        stdlib_and_self = {
            "enum", "csv", "logging", "os", "socket", "threading", "time",
            "json", "base64", "struct", "sys", "__future__", "roonapi", "typing",
            "thread", "_thread",
        }
        # websocket is the primary runtime dependency; simplejson is conditionally
        # imported with a fallback to stdlib json, so it's optional.
        self.assertEqual(found - stdlib_and_self, {"websocket", "simplejson"})


if __name__ == "__main__":
    unittest.main()
