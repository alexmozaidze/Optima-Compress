"""Run after SLINT_EMIT_DEBUG_INFO=1 cargo build --features slint/mcp."""

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        environment = {**os.environ, "SLINT_BACKEND": "headless", "SLINT_MCP_PORT": str(port)}
        self.process = subprocess.Popen(
            [ROOT / "target/debug/slint-rust-template"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_app)
        deadline = time.monotonic() + 10
        while True:
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode())
            try:
                self.window = self.call("list_windows")["windowHandles"][0]
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self.root = self.call("get_window_properties", windowHandle=self.window)["rootElementHandle"]
        elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
        self.rows = [element["handle"] for element in elements if element.get("accessibleRole") == "ListItem"]
        self.assertEqual(len(self.rows), 3)

    def stop_app(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stderr.close()

    def call(self, name, **arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def find(self, identifier):
        deadline = time.monotonic() + 2
        while True:
            handles = self.call("find_elements_by_id", windowHandle=self.window, elementsId=identifier).get("elementHandles", [])
            if handles:
                return handles[0]
            if time.monotonic() >= deadline:
                self.fail(f"Element did not become available: {identifier}")
            time.sleep(0.02)

    def click(self, element, **modifiers):
        self.call("click_element", elementHandle=element, modifiers=modifiers)

    def selected(self):
        return [self.call("get_element_properties", elementHandle=row).get("accessibleItemSelected", False) for row in self.rows]

    def labels(self, element=None):
        elements = self.call("get_element_tree", elementHandle=element or self.root, maxElements=1000)["elements"]
        return [entry.get("accessibleLabel", "") for entry in elements]

    def choose(self, bar, label):
        elements = self.call("get_element_tree", elementHandle=self.find(bar), maxElements=100)["elements"]
        option = next(entry for entry in elements if entry.get("accessibleLabel") == label)
        self.click(option["handle"])

    def box(self, start=None, target=None, **modifiers):
        self.call("drag_element", elementHandle=start or self.find("VideoList::selection-touch"), target=target or {"x": 400, "y": 150}, modifiers=modifiers)

    def test_plain_click_and_control_toggle(self):
        self.click(self.rows[0])
        self.assertEqual(self.selected(), [True, False, False])
        self.assertIn("1 video selected", self.labels())
        self.click(self.rows[2], control=True)
        self.assertEqual(self.selected(), [True, False, True])
        self.assertIn("2 videos selected", self.labels())
        self.click(self.rows[0], control=True)
        self.assertEqual(self.selected(), [False, False, True])
        self.click(self.rows[1])
        self.assertEqual(self.selected(), [False, True, False])

    def test_shift_range_preserves_anchor(self):
        self.click(self.rows[2])
        self.click(self.rows[0], shift=True)
        self.assertEqual(self.selected(), [True, True, True])
        self.click(self.rows[1], shift=True)
        self.assertEqual(self.selected(), [False, True, True])

    def test_control_shift_adds_a_range(self):
        self.click(self.rows[0])
        self.click(self.rows[2], control=True)
        self.click(self.rows[1], control=True, shift=True)
        self.assertEqual(self.selected(), [True, True, True])

    def test_reverse_box_and_control_box_toggle(self):
        self.box()
        self.assertEqual(self.selected(), [False, True, True])
        self.box(start=self.rows[2], target={"x": 5, "y": 150}, control=True)
        self.assertEqual(self.selected(), [False, False, False])

    def test_shift_box_adds_without_clearing(self):
        self.click(self.rows[0])
        self.box(start=self.rows[1], target={"x": 5, "y": 300}, shift=True)
        self.assertEqual(self.selected(), [True, True, True])

    def test_clear_button_and_escape(self):
        self.click(self.rows[0])
        self.click(self.find("SelectionSettings::clear-touch"))
        self.assertEqual(self.selected(), [False, False, False])
        self.click(self.rows[1])
        self.call("dispatch_key_event", windowHandle=self.window, text="\u001b")
        self.assertEqual(self.selected(), [False, False, False])

    def test_edits_only_change_selected_videos_and_survive_deselection(self):
        self.click(self.rows[0])
        self.click(self.rows[2], control=True)
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.choose("SelectionSettings::size-bar", "Custom")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::size-input"), value="12")
        self.assertIn("AV1 · 720p", self.labels(self.rows[0]))
        self.assertIn("12 MB target", self.labels(self.rows[0]))
        self.assertIn("H.264 · 720p", self.labels(self.rows[1]))
        self.assertIn("20 MB target", self.labels(self.rows[1]))
        self.assertIn("AV1 · 720p", self.labels(self.rows[2]))
        self.assertIn("12 MB target", self.labels(self.rows[2]))
        self.click(self.rows[1])
        self.click(self.rows[2], control=True)
        self.assertTrue(any(label.startswith("Mixed settings") for label in self.labels()))
        self.choose("SelectionSettings::resolution-bar", "1080p")
        self.assertIn("AV1 · 720p", self.labels(self.rows[0]))
        self.assertIn("H.264 · 1080p", self.labels(self.rows[1]))
        self.assertIn("AV1 · 1080p", self.labels(self.rows[2]))
        self.assertIn("12 MB target", self.labels(self.rows[2]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
