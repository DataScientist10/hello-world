"""The operator console served by the hub itself.

Serving the page from the hub is not a convenience -- it is the only way it
works. The depot has no internet, so there is no CDN to load from; and the page
must reach the API from the same origin, because there is no cross-origin story
to negotiate on an air-gapped LAN.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iot_hub.auth import HEADER_OPERATOR                 # noqa: E402
from tests.support import OPERATOR_KEY, HubTestCase      # noqa: E402

VIEWER_KEY = "test-viewer-key"
CONSOLE = Path(__file__).resolve().parent.parent / "iot_hub" / "console.html"


class ConsoleServingTests(HubTestCase):
    config_overrides = {"viewer_key": VIEWER_KEY}

    def get_console(self):
        from iot_hub.api import Request
        return self.api.handle(Request("GET", "/console", {}, b""))

    def test_console_is_served_as_html(self):
        response = self.get_console()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.content_type.startswith("text/html"))
        self.assertIn(b"<!doctype html>", response.body[:64].lower())

    def test_console_needs_no_credential_to_load(self):
        """The page is inert markup; it carries no data until a key is supplied."""
        self.assertEqual(self.get_console().status, 200)

    def test_console_embeds_no_key(self):
        """A page that shipped a key would hand full read access to anyone who can GET it."""
        body = self.get_console().body
        self.assertNotIn(OPERATOR_KEY.encode(), body)
        self.assertNotIn(VIEWER_KEY.encode(), body)

    def test_console_sends_a_restrictive_csp(self):
        headers = self.get_console().headers
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_console_can_be_disabled(self):
        self.config.console_enabled = False
        self.assertEqual(self.get_console().status, 404)

    def test_console_is_reachable_alongside_the_probes(self):
        for path in ("/healthz", "/readyz", "/metrics", "/v1/time", "/console"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 200)


class ConsoleContentTests(unittest.TestCase):
    """Constraints the file itself has to honour to work at a depot."""

    @classmethod
    def setUpClass(cls):
        cls.html = CONSOLE.read_text()
        # Assert against code, not prose: the file's own comments discuss the
        # very APIs these tests ban, and a mention is not a use.
        stripped = re.sub(r"<!--.*?-->", "", cls.html, flags=re.S)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
        stripped = re.sub(r"(?m)^\s*//.*$", "", stripped)
        stripped = re.sub(r"(?m)\s//[^\n\"']*$", "", stripped)
        cls.code = stripped

    def assertAbsent(self, needle, why):
        """Fail with the offending line, not a dump of the whole file."""
        for number, line in enumerate(self.code.splitlines(), 1):
            if needle in line:
                self.fail(f"{why} — {needle} at console.html:{number}: {line.strip()[:90]}")

    def test_no_external_resources(self):
        """No internet at the depot: an off-origin URL would fail silently."""
        external = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', self.code)
        self.assertEqual(external, [], f"console must not load anything off-origin: {external}")

    def test_no_webfont_link(self):
        self.assertAbsent("fonts.googleapis.com", "no internet at the depot")
        self.assertAbsent("@font-face", "webfonts cannot be fetched at the depot")

    def test_api_data_is_never_written_as_markup(self):
        """Fault codes and firmware come from vehicles; innerHTML here would be stored XSS."""
        for needle in ("innerHTML", "outerHTML", "document.write"):
            self.assertAbsent(needle, "vehicle-supplied strings must be rendered with textContent")

    def test_no_dynamic_code_evaluation(self):
        for needle in ("eval(", "new Function("):
            self.assertAbsent(needle, "the console must not evaluate code at runtime")

    def test_key_is_held_per_tab_not_persisted(self):
        """sessionStorage dies with the tab; localStorage would outlive the shift."""
        self.assertIn("sessionStorage", self.code)
        self.assertAbsent("localStorage", "an operator key must not outlive the browser tab")

    def test_attention_panel_has_a_resting_state(self):
        """A blank panel is ambiguous: broken, still loading, or genuinely clear?

        An operations console spends most of its life with nothing to flag, so
        the quiet state is the one an operator sees most and has to trust.
        """
        self.assertIn("All ", self.code)
        self.assertIn("vehicles nominal", self.code)
        self.assertIn("no faults, no low battery, no stale reports", self.code)

    def test_quiet_and_silent_are_told_apart(self):
        """A fleet with nothing wrong and a fleet saying nothing are not the same."""
        self.assertIn("No vehicles reporting", self.code)
        self.assertIn("nothing has uploaded telemetry yet", self.code)
        self.assertIn("empty clear", self.code)
        self.assertIn("empty idle", self.code)

    def test_console_only_calls_read_endpoints(self):
        """A read-only key must be enough; anything else would demand the write key."""
        calls = re.findall(r'api\("(/v1/[^"]+)"', self.code)
        self.assertTrue(calls, "expected the console to call the API")
        for path in calls:
            with self.subTest(path=path):
                self.assertIn(path.split("?")[0],
                              ("/v1/fleet", "/v1/fleet/map", "/v1/status", "/v1/events"))


if __name__ == "__main__":
    unittest.main()
