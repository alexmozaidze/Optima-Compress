"""Run after SLINT_EMIT_DEBUG_INFO=1 cargo build --features slint/mcp.

AudioOverflowTests loads fixture data through slint-viewer, so it needs slint-viewer and Xvfb;
it skips when either is missing.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def start_virtual_display():
    read_descriptor, write_descriptor = os.pipe()
    process = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_descriptor), "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
        pass_fds=[write_descriptor],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write_descriptor)
    with os.fdopen(read_descriptor) as stream:
        number = stream.readline().strip()
    if not number:
        process.kill()
        process.wait(timeout=5)
        raise unittest.SkipTest("Xvfb did not report a display")
    return process, f":{number}"


class AppWindowTests(unittest.TestCase):
    startup_timeout = 10
    expected_row_count = 3

    def command(self):
        return [ROOT / "target/debug/slint-rust-template"]

    def environment(self, port):
        return {**os.environ, "SLINT_BACKEND": "headless", "SLINT_MCP_PORT": str(port)}

    def setUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.process = subprocess.Popen(
            self.command(),
            cwd=ROOT,
            env=self.environment(port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_app)
        deadline = time.monotonic() + self.startup_timeout
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
        self.rows = self.wait_for_rows()

    def wait_for_rows(self):
        deadline = time.monotonic() + self.startup_timeout
        rows = []
        while True:
            elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
            for element in elements:
                if element.get("accessibleRole") == "ListItem" and element["handle"] not in rows:
                    rows.append(element["handle"])
            if len(rows) == self.expected_row_count:
                self.call("scroll_element", elementHandle=self.find("VideoList::list-scroll"), deltaY=10000, deltaX=0)
                return rows
            if time.monotonic() >= deadline:
                self.fail(f"Expected {self.expected_row_count} video rows, found {len(rows)}")
            if rows:
                viewport = self.find("VideoList::list-scroll")
                height = self.call("get_element_properties", elementHandle=viewport)["size"]["height"]
                self.call("scroll_element", elementHandle=viewport, deltaY=-height / 2, deltaX=0)
            time.sleep(0.02)

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

    def reveal_row(self, element):
        row = self.call("get_element_properties", elementHandle=element)
        viewport = self.call("get_element_properties", elementHandle=self.find("VideoList::list-scroll"))
        top = viewport["absolutePosition"]["y"]
        bottom = top + viewport["size"]["height"]
        row_top = row["absolutePosition"]["y"]
        row_bottom = row_top + row["size"]["height"]
        delta = top - row_top if row_top < top else min(0, bottom - row_bottom)
        if delta:
            self.call(
                "dispatch_pointer_scroll", windowHandle=self.window,
                position={"x": 1, "y": top + 1}, deltaY=delta, deltaX=0,
            )

    def click(self, element, **modifiers):
        if element in self.rows:
            self.reveal_row(element)
        self.call("click_element", elementHandle=element, modifiers=modifiers)

    def selected(self):
        return [self.call("get_element_properties", elementHandle=row).get("accessibleItemSelected", False) for row in self.rows]

    def labels(self, element=None):
        if element in self.rows:
            self.reveal_row(element)
        elements = self.call("get_element_tree", elementHandle=element or self.root, maxElements=1000)["elements"]
        return [entry.get("accessibleLabel", "") for entry in elements]

    def choose(self, bar, label):
        self.scroll_settings(10000)
        elements = self.call("get_element_tree", elementHandle=self.find(bar), maxElements=100)["elements"]
        option = next(entry for entry in elements if entry.get("accessibleLabel") == label)
        self.click(option["handle"])

    def checked_options(self, bar):
        elements = self.call("get_element_tree", elementHandle=self.find(bar), maxElements=100)["elements"]
        return [
            entry["accessibleLabel"] for entry in elements
            if entry.get("accessibleRole") == "RadioButton" and entry.get("accessibleChecked", False)
        ]

    def assert_no_segmented_selection(self):
        controls = self.menu_controls()
        self.assertTrue(controls)
        for control in controls:
            self.assertFalse(control.get("accessibleEnabled", False), control)
            self.assertFalse(control.get("accessibleChecked", False), control)
        self.assertNotIn("No videos selected", self.labels())
        for bar in ["codec-bar", "frame-rate-bar", "size-bar", "resolution-bar"]:
            self.assertEqual(self.checked_options(f"SelectionSettings::{bar}"), [])

    def value(self, identifier):
        return self.call("get_element_properties", elementHandle=self.find(identifier)).get("accessibleValue", "")

    def codec_highlight(self):
        elements = self.call("get_element_tree", elementHandle=self.find("SelectionSettings::codec-bar"), maxElements=100)["elements"]
        pill = next(
            entry for entry in elements
            if any(item.get("id") == "SegmentedBar::selection-pill" for item in entry.get("typeNamesAndIds", []))
        )
        selected = next(
            (entry for entry in elements if entry.get("accessibleRole") == "RadioButton" and entry.get("accessibleChecked", False)),
            None,
        )
        return pill, selected

    def horizontal_center(self, element):
        return element.get("absolutePosition", {}).get("x", 0) + element["size"]["width"] / 2

    def wait_for_opacity(self, element, opacity):
        deadline = time.monotonic() + 2
        while True:
            properties = self.call("get_element_properties", elementHandle=element)
            if properties.get("computedOpacity", 0) == opacity:
                return properties
            self.assertLess(time.monotonic(), deadline, f"Element opacity did not reach {opacity}")
            time.sleep(0.02)

    def menu_controls(self):
        elements = self.call("get_element_tree", elementHandle=self.find("AppWindow::settings-panel"), maxElements=1000)["elements"]
        return [entry for entry in elements if entry.get("accessibleRole") in ["RadioButton", "Checkbox", "TextInput"]]

    def scroll_settings(self, delta):
        scroll = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        position = {
            "x": scroll["absolutePosition"].get("x", 0) + scroll["size"]["width"] / 2,
            "y": scroll["absolutePosition"]["y"] + 1,
        }
        self.call("dispatch_pointer_scroll", windowHandle=self.window, position=position, deltaY=delta, deltaX=0)

    def audio_tracks(self):
        self.scroll_settings(-10000)
        return [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]

    def audio_track(self, label):
        return next(track for track in self.audio_tracks() if track.get("accessibleLabel") == label)

    def assert_audio_viewport(self, expected_labels):
        self.scroll_settings(10000)
        codec = self.find("SelectionSettings::codec-bar")
        self.scroll_settings(-10000)
        scroll = self.find("SelectionSettings::audio-scroll")
        self.call("scroll_element", elementHandle=scroll, deltaY=10000, deltaX=0)
        first = self.audio_tracks()
        self.assertEqual([track["accessibleLabel"] for track in first], expected_labels[:len(first)])
        self.assertEqual(first[0]["accessibleLabel"], expected_labels[0])
        viewport = self.call("get_element_properties", elementHandle=scroll)
        self.assertLess(
            viewport["size"]["height"],
            len(expected_labels) * first[0]["size"]["height"] + len(expected_labels) - 1,
        )
        codec_position = self.call("get_element_properties", elementHandle=codec)["absolutePosition"]
        position = {
            "x": viewport["absolutePosition"].get("x", 0) + viewport["size"]["width"] / 2,
            "y": viewport["absolutePosition"]["y"] + viewport["size"]["height"] / 2,
        }
        self.call("dispatch_pointer_scroll", windowHandle=self.window, position=position, deltaY=-10000, deltaX=0)
        last = self.audio_tracks()
        self.assertEqual([track["accessibleLabel"] for track in last], expected_labels[-len(last):])
        self.assertEqual(last[-1]["accessibleLabel"], expected_labels[-1])
        self.assertNotEqual(
            [(track["accessibleLabel"], track["absolutePosition"]["y"]) for track in first],
            [(track["accessibleLabel"], track["absolutePosition"]["y"]) for track in last],
        )
        self.assertEqual(self.call("get_element_properties", elementHandle=codec)["absolutePosition"], codec_position)
        self.assertEqual(self.call("get_element_properties", elementHandle=scroll)["size"], viewport["size"])

    def box(self, start=None, target_row=None, target_x=400, **modifiers):
        start = start or self.rows[2]
        self.reveal_row(start)
        target = self.call("get_element_properties", elementHandle=target_row or self.rows[1])
        viewport = self.call("get_element_properties", elementHandle=self.find("VideoList::list-scroll"))
        top = target["absolutePosition"]["y"]
        bottom = top + target["size"]["height"]
        target_y = top + target["size"]["height"] / 2
        if bottom < viewport["absolutePosition"]["y"]:
            target_y = bottom - 1
        if top > viewport["absolutePosition"]["y"] + viewport["size"]["height"]:
            target_y = top + 1
        self.call("drag_element", elementHandle=start, target={"x": target_x, "y": target_y}, modifiers=modifiers)

class SelectionTests(AppWindowTests):
    def test_frame_rate_sits_to_the_right_of_codec(self):
        self.click(self.rows[0])
        self.scroll_settings(10000)
        codec = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::codec-bar"))
        frame_rate = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::frame-rate-bar"))
        self.assertAlmostEqual(codec["absolutePosition"]["y"], frame_rate["absolutePosition"]["y"], delta=1)
        self.assertGreater(frame_rate["absolutePosition"]["x"], codec["absolutePosition"].get("x", 0) + codec["size"]["width"])
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["60"])

    def test_frame_rate_edits_persist_per_video_and_only_share_the_edited_field(self):
        self.click(self.rows[0])
        panel = self.find("AppWindow::settings-panel")
        initial = self.call("get_element_properties", elementHandle=panel)
        self.choose("SelectionSettings::frame-rate-bar", "15")
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["15"])
        self.click(self.rows[1])
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["60"])
        self.click(self.rows[0], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), [])
        self.assertTrue(any(label.startswith("Mixed settings") for label in self.labels()))
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), [])
        self.choose("SelectionSettings::frame-rate-bar", "Custom")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::frame-rate-input"), value="45")
        self.click(self.rows[0])
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(shown["size"]["width"], initial["size"]["width"])
        self.assertAlmostEqual(shown["size"]["height"], initial["size"]["height"], delta=1)
        self.assertAlmostEqual(shown["absolutePosition"]["y"], initial["absolutePosition"]["y"], delta=1)
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["Custom"])
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "45")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::frame-rate-input"), value="48")
        self.click(self.rows[1], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["Custom"])
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "")
        self.choose("SelectionSettings::size-bar", "25 MB")
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "")
        self.click(self.rows[0])
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "48")
        self.click(self.rows[1])
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "45")
        self.choose("SelectionSettings::frame-rate-bar", "60")
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["60"])
        self.click(self.find("AppWindow::clear-touch"))
        self.assert_no_segmented_selection()
        self.click(self.rows[0])
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), ["Custom"])
        self.assertEqual(self.value("SelectionSettings::frame-rate-input"), "48")

    def test_selection_toolbar_sits_above_the_videos_not_in_the_settings(self):
        self.click(self.rows[1])
        toolbar = self.find("AppWindow::selection-toolbar")
        bounds = self.call("get_element_properties", elementHandle=toolbar)
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        self.assertAlmostEqual(bounds["absolutePosition"]["y"] + bounds["size"]["height"], videos["absolutePosition"]["y"], delta=0.1)
        self.assertIn("1 video selected", self.labels(toolbar))
        self.assertIn("Clear", self.labels(toolbar))
        panel = self.find("AppWindow::settings-panel")
        self.assertNotIn("1 video selected", self.labels(panel))
        self.assertNotIn("Changes apply only to selected videos", self.labels())
        self.click(self.find("AppWindow::clear-touch"))
        self.assert_no_segmented_selection()

    def test_populated_list_never_shows_the_empty_notice(self):
        self.assertNotIn("No videos selected", self.labels())
        self.click(self.rows[0])
        self.assertNotIn("No videos selected", self.labels())
        self.click(self.find("AppWindow::clear-touch"))
        self.assert_no_segmented_selection()

    def test_deselection_dims_settings_without_hiding_or_resizing_them(self):
        panel = self.find("AppWindow::settings-panel")
        initial = self.call("get_element_properties", elementHandle=panel)
        scroll = self.find("SelectionSettings::settings-scroll")
        self.assertAlmostEqual(self.call("get_element_properties", elementHandle=scroll)["computedOpacity"], 0.45)
        self.click(self.rows[0])
        self.assertEqual(self.call("get_element_properties", elementHandle=scroll)["computedOpacity"], 1)
        self.click(self.find("AppWindow::clear-touch"))
        self.assertAlmostEqual(self.call("get_element_properties", elementHandle=scroll)["computedOpacity"], 0.45)
        self.assert_no_segmented_selection()
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(shown["size"], initial["size"])
        self.assertEqual(shown["absolutePosition"], initial["absolutePosition"])

    def test_empty_selection_hides_segmented_indicators_without_resetting_options(self):
        self.assert_no_segmented_selection()
        self.click(self.rows[0])
        options = [
            ("codec-bar", "Quality (AV1)"),
            ("size-bar", "25 MB"),
            ("resolution-bar", "1080p"),
        ]
        for bar, label in options:
            self.choose(f"SelectionSettings::{bar}", label)
        for method in ["clear-button", "escape", "control-click"]:
            with self.subTest(method=method):
                if method == "clear-button":
                    self.click(self.find("AppWindow::clear-touch"))
                elif method == "escape":
                    self.call("dispatch_key_event", windowHandle=self.window, text="\u001b")
                else:
                    self.click(self.rows[0], control=True)
                self.assert_no_segmented_selection()
                self.click(self.rows[0])
                for bar, label in options:
                    self.assertEqual(self.checked_options(f"SelectionSettings::{bar}"), [label])

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

    def test_empty_selection_keeps_settings_visible_and_disabled(self):
        window = self.call("get_element_properties", elementHandle=self.root)
        panel = self.find("AppWindow::settings-panel")
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertAlmostEqual(shown["absolutePosition"]["y"] + shown["size"]["height"], window["size"]["height"], delta=0.1)
        self.assertEqual(shown.get("computedOpacity", 0), 1)
        self.assertEqual(shown.get("absolutePosition", {}).get("x", 0), 0)
        self.assertEqual(shown["size"]["width"], window["size"]["width"])
        self.assert_no_segmented_selection()
        self.assertFalse(self.call("find_elements_by_id", windowHandle=self.window, elementsId="AppWindow::clear-touch").get("elementHandles", []))
        for label in ["CODEC", "TARGET SIZE", "RESOLUTION", "AUDIO"]:
            self.assertIn(label, self.labels())
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.click(self.rows[0])
        self.assertEqual(self.checked_options("SelectionSettings::codec-bar"), ["Compatibility (H.264)"])

    def test_selection_enables_the_menu_and_custom_fields_without_resizing_it(self):
        panel = self.find("AppWindow::settings-panel")
        initial = self.call("get_element_properties", elementHandle=panel)
        self.click(self.rows[0])
        self.wait_for_opacity(self.find("AppWindow::settings-menu"), 1)
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(shown["absolutePosition"], initial["absolutePosition"])
        self.assertEqual(shown["size"], initial["size"])
        scroll = self.find("SelectionSettings::settings-scroll")
        self.assertEqual(self.call("get_element_properties", elementHandle=scroll)["computedOpacity"], 1)
        self.choose("SelectionSettings::size-bar", "Custom")
        self.choose("SelectionSettings::resolution-bar", "Custom")
        fields = [self.find(f"SelectionSettings::{identifier}") for identifier in ["size-input", "custom-width", "custom-height"]]
        self.click(self.find("AppWindow::clear-touch"))
        self.assertEqual(self.selected(), [False, False, False])
        self.assertEqual(self.find("AppWindow::settings-panel"), panel)
        self.assert_no_segmented_selection()
        expanded = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(expanded["size"], initial["size"])
        self.assertEqual(expanded["absolutePosition"], initial["absolutePosition"])
        self.assertAlmostEqual(
            expanded["absolutePosition"]["y"] + expanded["size"]["height"],
            initial["absolutePosition"]["y"] + initial["size"]["height"],
        )
        self.click(self.rows[0])
        for field in fields:
            self.assertFalse(self.call("get_element_properties", elementHandle=field).get("accessibleReadOnly", False))
        for control in self.menu_controls():
            self.assertTrue(control.get("accessibleEnabled", False), control)
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::size-input"), value="16")
        self.assertIn("16 MB target", self.labels(self.rows[0]))
        self.assertIn("20 MB target", self.labels(self.rows[1]))

    def test_menu_leaves_the_video_list_scrollable(self):
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        self.assertGreater(videos["size"]["height"], 0)
        self.call("scroll_element", elementHandle=self.find("VideoList::list-scroll"), deltaY=-10000, deltaX=0)
        last = self.call("get_element_properties", elementHandle=self.rows[-1])
        self.assertLessEqual(
            last["absolutePosition"]["y"] + last["size"]["height"],
            videos["absolutePosition"]["y"] + videos["size"]["height"],
        )

    def test_menu_reserves_full_content_height_without_scrolling_the_settings(self):
        panel = self.find("AppWindow::settings-panel")
        initial = self.call("get_element_properties", elementHandle=panel)
        self.click(self.rows[1])
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(shown["size"], initial["size"])
        self.assertEqual(shown["absolutePosition"], initial["absolutePosition"])
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-scroll"))
        self.assertLessEqual(
            audio["absolutePosition"]["y"] + audio["size"]["height"] + 31.5,
            shown["absolutePosition"]["y"] + shown["size"]["height"],
        )
        codec = self.find("SelectionSettings::codec-bar")
        before = self.call("get_element_properties", elementHandle=codec)
        self.scroll_settings(-10000)
        after = self.call("get_element_properties", elementHandle=codec)
        self.assertEqual(after["absolutePosition"], before["absolutePosition"])

    def test_row_selection_highlight_fades_in_and_out(self):
        self.click(self.rows[0])
        elements = self.call("get_element_tree", elementHandle=self.rows[0], maxElements=100)["elements"]
        highlight = next(
            entry["handle"] for entry in elements
            if any(item.get("id") == "ClipRow::selection-highlight" for item in entry.get("typeNamesAndIds", []))
        )
        entering = self.call("get_element_properties", elementHandle=highlight).get("computedOpacity", 0)
        self.assertGreater(entering, 0)
        self.assertLess(entering, 1)
        self.assertEqual(self.selected(), [True, False, False])
        self.wait_for_opacity(highlight, 1)
        self.click(self.rows[0], control=True)
        leaving = self.call("get_element_properties", elementHandle=highlight).get("computedOpacity", 0)
        self.assertGreater(leaving, 0)
        self.assertLess(leaving, 1)
        self.assertEqual(self.selected(), [False, False, False])
        self.wait_for_opacity(highlight, 0)

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
        self.box(start=self.rows[2], target_x=5, control=True)
        self.assertEqual(self.selected(), [False, False, False])

    def test_settings_follow_selection_before_box_drag_finishes(self):
        self.assertNotIn("1 video selected", self.labels())
        observed_counts = set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            drag = executor.submit(self.box, target_row=self.rows[0])
            while not drag.done():
                elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
                if drag.done():
                    break
                selected_count = sum(
                    entry.get("accessibleItemSelected", False)
                    for entry in elements if entry.get("accessibleRole") == "ListItem"
                )
                if selected_count > 0:
                    label = "1 video selected" if selected_count == 1 else f"{selected_count} videos selected"
                    self.assertIn(label, [entry.get("accessibleLabel", "") for entry in elements])
                    observed_counts.add(selected_count)
                time.sleep(0.02)
            drag.result()
        self.assertEqual(observed_counts, {1, 2, 3})
        self.assertEqual(self.selected(), [True, True, True])
        self.assertIn("3 videos selected", self.labels())

    def test_shift_box_adds_without_clearing(self):
        self.click(self.rows[0])
        self.box(start=self.rows[1], target_row=self.rows[2], target_x=5, shift=True)
        self.assertEqual(self.selected(), [True, True, True])

    def test_clear_button_and_escape(self):
        self.click(self.rows[0])
        self.click(self.find("AppWindow::clear-touch"))
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

    def test_conflicting_segments_are_unselected_without_clearing_shared_settings(self):
        self.click(self.rows[0])
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.choose("SelectionSettings::size-bar", "25 MB")
        self.choose("SelectionSettings::resolution-bar", "1080p")
        self.click(self.rows[1], control=True)
        for bar in ["codec-bar", "size-bar", "resolution-bar"]:
            self.assertEqual(self.checked_options(f"SelectionSettings::{bar}"), [])
        self.click(self.rows[2], control=True)
        for bar in ["codec-bar", "size-bar", "resolution-bar"]:
            self.assertEqual(self.checked_options(f"SelectionSettings::{bar}"), [])
        self.click(self.rows[2], control=True)
        self.assertIn("AV1 · 1080p", self.labels(self.rows[0]))
        self.assertIn("H.264 · 720p", self.labels(self.rows[1]))
        self.choose("SelectionSettings::codec-bar", "Compatibility (H.264)")
        self.assertEqual(self.checked_options("SelectionSettings::codec-bar"), ["Compatibility (H.264)"])
        self.assertEqual(self.checked_options("SelectionSettings::size-bar"), [])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), [])
        self.click(self.rows[1], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::size-bar"), ["25 MB"])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), ["1080p"])
        self.click(self.rows[1])
        self.assertEqual(self.checked_options("SelectionSettings::size-bar"), ["20 MB"])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), ["720p"])

    def test_matching_segments_remain_selected_in_a_mixed_selection(self):
        self.click(self.rows[2])
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.click(self.rows[0], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::codec-bar"), [])
        self.assertEqual(self.checked_options("SelectionSettings::size-bar"), ["20 MB"])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), ["720p"])

    def test_highlight_appears_at_selected_option_after_mixed_selection(self):
        self.click(self.rows[0])
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.click(self.rows[1], control=True)
        deadline = time.monotonic() + 2
        while self.codec_highlight()[0].get("computedOpacity", 0) > 0:
            self.assertLess(time.monotonic(), deadline, "Mixed selection highlight did not disappear")
            time.sleep(0.02)
        self.choose("SelectionSettings::codec-bar", "Compatibility (H.264)")
        pill, selected = self.codec_highlight()
        self.assertIsNotNone(selected)
        self.assertAlmostEqual(self.horizontal_center(pill), self.horizontal_center(selected), delta=1)

    def test_highlight_still_slides_between_selected_options(self):
        self.click(self.rows[0])
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        pill, selected = self.codec_highlight()
        self.assertIsNotNone(selected)
        self.assertGreater(self.horizontal_center(pill), self.horizontal_center(selected) + 1)
        deadline = time.monotonic() + 2
        while abs(self.horizontal_center(pill) - self.horizontal_center(selected)) > 1:
            self.assertLess(time.monotonic(), deadline, "Highlight did not reach the selected option")
            time.sleep(0.02)
            pill, selected = self.codec_highlight()

    def test_conflicting_custom_values_are_blank_and_edits_preserve_other_values(self):
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        self.choose("SelectionSettings::size-bar", "Custom")
        self.choose("SelectionSettings::resolution-bar", "Custom")
        self.click(self.rows[0])
        for identifier, value in [("size-input", "12"), ("custom-width", "1280"), ("custom-height", "720")]:
            self.call("set_element_value", elementHandle=self.find(f"SelectionSettings::{identifier}"), value=value)
        self.click(self.rows[1], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::size-bar"), ["Custom"])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), ["Custom"])
        for identifier in ["size-input", "custom-width", "custom-height"]:
            self.assertEqual(self.value(f"SelectionSettings::{identifier}"), "")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::custom-width"), value="1600")
        self.assertEqual(self.value("SelectionSettings::custom-width"), "1600")
        self.assertEqual(self.value("SelectionSettings::custom-height"), "")
        self.assertIn("H.264 · 1600 × 720", self.labels(self.rows[0]))
        self.assertIn("H.264 · 1600 × 1080", self.labels(self.rows[1]))
        self.assertIn("12 MB target", self.labels(self.rows[0]))
        self.assertIn("8 MB target", self.labels(self.rows[1]))
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::size-input"), value="16")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::custom-height"), value="900")
        for row in self.rows[:2]:
            self.assertIn("H.264 · 1600 × 900", self.labels(row))
            self.assertIn("16 MB target", self.labels(row))
        self.assertFalse(any(label.startswith("Mixed settings") for label in self.labels()))

    def test_initial_audio_viewport_matches_the_selected_video(self):
        for index, labels in [
            (0, []),
            (1, ["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio"]),
            (2, ["Desktop audio", "Microphone"]),
        ]:
            with self.subTest(video=index):
                self.click(self.rows[index])
                visible = [track["accessibleLabel"] for track in self.audio_tracks()]
                self.assertEqual(visible, labels[:len(visible)])
                self.assertEqual(bool(visible), bool(labels))
                self.assertIn("AUDIO", self.labels())

    def test_audio_list_remains_visible_for_a_selected_video_without_tracks(self):
        self.click(self.rows[0])
        self.assertIn("AUDIO", self.labels())
        scroll = self.find("SelectionSettings::audio-scroll")
        empty = self.call("get_element_properties", elementHandle=scroll)
        self.assertGreater(empty["size"]["height"], 0)
        self.assertEqual(self.audio_tracks(), [])
        self.click(self.rows[0])
        self.assertIn("AUDIO", self.labels())
        self.assertEqual(self.audio_tracks(), [])
        self.assertEqual(self.call("get_element_properties", elementHandle=scroll)["size"], empty["size"])
        self.click(self.rows[1])
        self.assertTrue(self.audio_tracks())
        self.assertEqual(self.call("get_element_properties", elementHandle=scroll)["size"], empty["size"])
        self.click(self.find("AppWindow::clear-touch"))
        self.assertIn("AUDIO", self.labels())
        self.assert_no_segmented_selection()

    def test_demo_audio_scrolls_without_moving_the_settings(self):
        self.click(self.rows[1])
        self.assert_audio_viewport(["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio"])
        commentary = self.audio_track("Commentary")
        self.click(commentary["handle"])
        self.assertTrue(self.audio_track("Commentary")["accessibleChecked"])

    def test_audio_uses_the_space_reserved_for_collapsed_custom_fields(self):
        self.click(self.rows[1])
        panel = self.call("get_element_properties", elementHandle=self.find("AppWindow::settings-panel"))
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-scroll"))
        gap = panel["absolutePosition"]["y"] + panel["size"]["height"] - audio["absolutePosition"]["y"] - audio["size"]["height"]
        self.assertAlmostEqual(gap, 32, delta=1.5)
        self.assertEqual(self.audio_tracks()[0]["size"]["height"], 41)

    def test_audio_container_stays_visible_while_tracks_scroll(self):
        self.click(self.rows[1])
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-scroll"))
        settings = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        bottom = settings["absolutePosition"]["y"] + settings["size"]["height"]
        self.assertLessEqual(audio["absolutePosition"]["y"] + audio["size"]["height"], bottom - 31.5)
        position = {
            "x": audio["absolutePosition"]["x"] + 100,
            "y": min(bottom - 2, audio["absolutePosition"]["y"] + 10),
        }
        self.call("dispatch_pointer_scroll", windowHandle=self.window, position=position, deltaY=-10000, deltaX=0)
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-scroll"))
        self.assertLessEqual(audio["absolutePosition"]["y"] + audio["size"]["height"], bottom - 31.5)
        self.assertGreaterEqual(audio["absolutePosition"]["y"], settings["absolutePosition"]["y"])
        tracks = [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]
        self.assertEqual(tracks[-1]["accessibleLabel"], "System audio")
        self.assertEqual(tracks[0]["size"]["height"], 41)

    def test_mixed_audio_only_compares_videos_that_have_the_track(self):
        self.click(self.rows[2])
        self.click(self.audio_track("Microphone")["handle"])
        self.click(self.rows[0], control=True)
        track = self.audio_track("Microphone")
        self.assertFalse(track.get("accessibleChecked", False))
        self.assertNotEqual(track.get("accessibleDescription"), "Mixed values")
        self.assertFalse(any(label.startswith("Mixed settings") for label in self.labels()))
        self.click(self.rows[1], control=True)
        track = self.audio_track("Microphone")
        self.assertFalse(track.get("accessibleChecked", False))
        self.assertEqual(track.get("accessibleDescription"), "Mixed values")
        self.assertTrue(any(label.startswith("Mixed settings") for label in self.labels()))
        self.click(track["handle"])
        self.assertTrue(self.audio_track("Microphone")["accessibleChecked"])
        for index in [1, 2]:
            self.click(self.rows[index])
            self.assertTrue(self.audio_track("Microphone")["accessibleChecked"])

    def test_audio_row_size_and_spacing_stay_consistent(self):
        measurements = []
        for row in self.rows[1:]:
            self.click(row)
            self.scroll_settings(10000)
            resolution = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::resolution-bar"))
            scroll = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
            self.scroll_settings(scroll["absolutePosition"]["y"] - resolution["absolutePosition"]["y"])
            resolution = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::resolution-bar"))
            track = next(control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox")
            gap = track["absolutePosition"]["y"] - resolution["absolutePosition"]["y"] - resolution["size"]["height"]
            measurements.append((track["size"]["height"], gap))
        self.assertEqual(measurements[0][0], measurements[1][0])
        self.assertAlmostEqual(measurements[0][1], measurements[1][1], delta=0.01)

    def test_audio_edits_preserve_availability_and_other_track_choices(self):
        self.click(self.rows[1])
        self.click(self.audio_track("Party chat")["handle"])
        self.click(self.rows[0], control=True)
        self.click(self.rows[2], control=True)
        self.assertGreaterEqual(len(self.audio_tracks()), 4)
        self.assertTrue(self.audio_track("Party chat")["accessibleChecked"])
        self.assertNotEqual(self.audio_track("Party chat").get("accessibleDescription"), "Mixed values")
        self.click(self.audio_track("Music")["handle"])
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.click(self.rows[0])
        self.assertEqual(self.audio_tracks(), [])
        self.click(self.rows[2])
        self.assertEqual([track["accessibleLabel"] for track in self.audio_tracks()], ["Desktop audio", "Microphone"])
        self.click(self.rows[1])
        self.assertTrue(self.audio_track("Music")["accessibleChecked"])
        self.assertTrue(self.audio_track("Party chat")["accessibleChecked"])


class ViewerTests(AppWindowTests):
    fixture = None
    startup_timeout = 20

    def command(self):
        return ["slint-viewer", "--auto-reload", "--load-data", str(self.fixture), "ui/app-window.slint"]

    def environment(self, port):
        environment = {key: value for key, value in os.environ.items() if key not in ["WAYLAND_DISPLAY", "WAYLAND_SOCKET"]}
        environment.update(DISPLAY=self.display, SLINT_BACKEND="winit-software", SLINT_MCP_PORT=str(port))
        return environment

    def setUp(self):
        if shutil.which("slint-viewer") is None or shutil.which("Xvfb") is None:
            self.skipTest("Needs slint-viewer and Xvfb to load fixture data")
        display, self.display = start_virtual_display()
        self.addCleanup(stop_virtual_display, display)
        super().setUp()


class AudioOverflowTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-overflow.json"

    def test_audio_list_scrolls_and_edits_beyond_four_tracks(self):
        self.click(self.rows[1])
        self.assert_audio_viewport(["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio"])
        scroll = self.find("SelectionSettings::audio-scroll")
        self.call("scroll_element", elementHandle=scroll, deltaY=-10000, deltaX=0)
        commentary = self.audio_track("Commentary")
        self.assertFalse(commentary.get("accessibleChecked", False))
        self.click(commentary["handle"])
        self.assertTrue(self.audio_track("Commentary")["accessibleChecked"])
        self.click(self.rows[2])
        self.assertEqual([track["accessibleLabel"] for track in self.audio_tracks()], ["Game audio"])


class ViewerSelectionStateTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-overflow.json"

    test_frame_rate_sits_to_the_right_of_codec = SelectionTests.test_frame_rate_sits_to_the_right_of_codec
    test_frame_rate_edits_persist_per_video_and_only_share_the_edited_field = SelectionTests.test_frame_rate_edits_persist_per_video_and_only_share_the_edited_field
    test_populated_list_never_shows_the_empty_notice = SelectionTests.test_populated_list_never_shows_the_empty_notice
    test_deselection_dims_settings_without_hiding_or_resizing_them = SelectionTests.test_deselection_dims_settings_without_hiding_or_resizing_them
    test_empty_selection_keeps_settings_visible_and_disabled = SelectionTests.test_empty_selection_keeps_settings_visible_and_disabled


class EmptyVideoListTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/empty-video-list.json"
    expected_row_count = 0

    def setUp(self):
        super().setUp()
        deadline = time.monotonic() + self.startup_timeout
        while self.call("get_element_properties", elementHandle=self.root)["size"] != {"width": 1141, "height": 762}:
            self.assertLess(time.monotonic(), deadline, "Viewer did not reach the fixture window size")
            time.sleep(0.02)

    def test_empty_notice_is_centered_in_video_list_not_over_settings(self):
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        notice = self.find("AppWindow::empty-state")
        shown = self.call("get_element_properties", elementHandle=notice)
        self.assertEqual(shown["absolutePosition"], videos["absolutePosition"])
        self.assertEqual(shown["size"], videos["size"])
        self.assertIn("No videos selected", self.labels(notice))
        self.assertTrue(any(label.startswith("Drag and drop videos") for label in self.labels(notice)))
        title = self.call("get_element_properties", elementHandle=self.find("AppWindow::empty-title"))
        instructions = self.call("get_element_properties", elementHandle=self.find("AppWindow::empty-instructions"))
        self.assertAlmostEqual(self.horizontal_center(title), self.horizontal_center(videos), delta=1)
        message_center = (title["absolutePosition"]["y"] + instructions["absolutePosition"]["y"] + instructions["size"]["height"]) / 2
        self.assertAlmostEqual(message_center, videos["absolutePosition"]["y"] + videos["size"]["height"] / 2, delta=1)
        panel_labels = self.labels(self.find("AppWindow::settings-panel"))
        self.assertNotIn("No videos selected", panel_labels)
        for label in ["CODEC", "TARGET SIZE", "RESOLUTION", "AUDIO"]:
            self.assertIn(label, panel_labels)
        controls = self.menu_controls()
        self.assertTrue(controls)
        self.assertTrue(all(not control.get("accessibleEnabled", False) for control in controls))


class AudioNameMismatchTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-name-mismatch.json"

    def test_differing_names_degrade_to_a_positional_row(self):
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        tracks = self.audio_tracks()
        self.assertEqual(
            [track["accessibleLabel"] for track in tracks],
            ["Different audio, track 1", "Microphone", "Party chat", "Different audio, track 4"],
        )
        self.assertEqual(tracks[0]["accessibleDescription"], "2 different names")
        self.assertEqual(tracks[1].get("accessibleDescription", ""), "")
        self.assertEqual(tracks[3]["accessibleDescription"], "2 different names; mixed values")
        self.assertIn("2 names", self.labels())
        self.assertTrue(any("edits apply by position" in label for label in self.labels()))

    def test_more_than_two_names_drop_the_count(self):
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        self.click(self.rows[2], control=True)
        tracks = self.audio_tracks()
        self.assertEqual(tracks[0]["accessibleDescription"], "Different names")
        self.assertEqual(tracks[3]["accessibleDescription"], "Different names; mixed values")
        self.assertIn("Names differ", self.labels())
        self.assertNotIn("2 names", self.labels())

    def test_differing_names_still_apply_by_position(self):
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        tracks = self.audio_tracks()
        self.click(tracks[0]["handle"])
        self.click(self.rows[0])
        self.assertFalse(self.audio_track("Game audio").get("accessibleChecked", False))
        self.click(self.rows[1])
        self.assertFalse(self.audio_track("Desktop audio").get("accessibleChecked", False))


def stop_virtual_display(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
