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

# 3.15rem at the 14px base font size.
AUDIO_ROW_HEIGHT = 44.1


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
        timeout = 30 if name == "drag_element" else 10
        with urllib.request.urlopen(request, timeout=timeout) as response:
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

    def assert_encoding(self, row, codec, resolution, size, frame_rate="60 FPS"):
        labels = self.labels(row)
        for value in [codec, frame_rate, resolution, size]:
            self.assertIn(value, labels)

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

    def wait_for_partial_opacity(self, element):
        deadline = time.monotonic() + 2
        while True:
            opacity = self.call("get_element_properties", elementHandle=element).get("computedOpacity", 0)
            if 0 < opacity < 1:
                return opacity
            self.assertLess(time.monotonic(), deadline, f"No intermediate opacity observed: {opacity}")
            time.sleep(0.02)

    def menu_controls(self):
        elements = self.call("get_element_tree", elementHandle=self.find("AppWindow::settings-panel"), maxElements=1000)["elements"]
        return [entry for entry in elements if entry.get("accessibleRole") in ["RadioButton", "Checkbox", "TextInput"]]

    def scroll_settings(self, delta):
        self.call("scroll_element", elementHandle=self.find("SelectionSettings::settings-scroll"), deltaY=delta, deltaX=0)

    def audio_tracks(self):
        self.scroll_settings(-10000)
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-list"))
        viewport = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        if audio["absolutePosition"]["y"] < viewport["absolutePosition"]["y"]:
            self.scroll_settings(viewport["absolutePosition"]["y"] - audio["absolutePosition"]["y"])
        return [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]

    def audio_track(self, label):
        tracks = self.audio_tracks()
        viewport = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        deadline = time.monotonic() + 2
        while True:
            match = next((track for track in tracks if track.get("accessibleLabel") == label), None)
            if match is not None:
                return match
            self.assertLess(time.monotonic(), deadline, f"Audio track did not become visible: {label}")
            self.scroll_settings(-viewport["size"]["height"] / 2)
            tracks = [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]

    def assert_audio_menu_scrolling(self, expected_labels):
        self.scroll_settings(10000)
        codec = self.find("SelectionSettings::codec-bar")
        audio = self.find("SelectionSettings::audio-list")
        rows = self.find("SelectionSettings::audio-rows")
        first = [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]
        self.assertEqual([track["accessibleLabel"] for track in first], expected_labels[:len(first)])
        self.assertEqual(first[0]["accessibleLabel"], expected_labels[0])
        section = self.call("get_element_properties", elementHandle=audio)
        rows_before = self.call("get_element_properties", elementHandle=rows)
        viewport = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        codec_position = self.call("get_element_properties", elementHandle=codec)["absolutePosition"]
        top = max(section["absolutePosition"]["y"], viewport["absolutePosition"]["y"])
        bottom = min(
            section["absolutePosition"]["y"] + section["size"]["height"],
            viewport["absolutePosition"]["y"] + viewport["size"]["height"],
        )
        position = {
            "x": section["absolutePosition"].get("x", 0) + section["size"]["width"] / 2,
            "y": (top + bottom) / 2,
        }
        self.call("dispatch_pointer_scroll", windowHandle=self.window, position=position, deltaY=-10000, deltaX=0)
        last = [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]
        self.assertEqual([track["accessibleLabel"] for track in last], expected_labels[-len(last):])
        self.assertEqual(last[-1]["accessibleLabel"], expected_labels[-1])
        self.assertNotEqual(
            [(track["accessibleLabel"], track["absolutePosition"]["y"]) for track in first],
            [(track["accessibleLabel"], track["absolutePosition"]["y"]) for track in last],
        )
        self.assertLess(self.call("get_element_properties", elementHandle=codec)["absolutePosition"]["y"], codec_position["y"])
        section_after = self.call("get_element_properties", elementHandle=audio)
        rows_after = self.call("get_element_properties", elementHandle=rows)
        self.assertEqual(section_after["size"], section["size"])
        self.assertAlmostEqual(
            rows_after["absolutePosition"]["y"] - section_after["absolutePosition"]["y"],
            rows_before["absolutePosition"]["y"] - section["absolutePosition"]["y"], delta=0.1,
        )

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
    def environment(self, port):
        environment = super().environment(port)
        if self._testMethodName in {
            "test_row_selection_highlight_fades_in_and_out",
            "test_highlight_still_slides_between_selected_options",
        }:
            environment["SLINT_SLOW_ANIMATIONS"] = "4"
        return environment

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
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        self.assertEqual(self.checked_options("SelectionSettings::frame-rate-bar"), [])
        self.choose("SelectionSettings::frame-rate-bar", "Custom")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::frame-rate-input"), value="45")
        self.click(self.rows[0])
        shown = self.call("get_element_properties", elementHandle=panel)
        self.assertEqual(shown["size"]["width"], initial["size"]["width"])
        self.assertAlmostEqual(
            shown["absolutePosition"]["y"] + shown["size"]["height"],
            initial["absolutePosition"]["y"] + initial["size"]["height"], delta=1,
        )
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

    def visible_file_names(self):
        elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
        return [entry["accessibleLabel"] for entry in elements if entry.get("accessibleRole") == "ListItem"]

    def press_row_action(self, label):
        elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
        action = next(entry for entry in elements if entry.get("accessibleLabel") == label)
        self.click(action["handle"])

    def test_delete_button_removes_that_video(self):
        toolbar = self.find("AppWindow::selection-toolbar")
        self.assertNotIn("video selected", self.labels(toolbar))
        self.press_row_action("Delete ShadowPlay_Clip_01.mp4")
        self.assertEqual(self.visible_file_names(), ["Ranked_Highlights.mp4", "Friend_Clip.mp4"])
        self.assertIn("2 videos", self.labels(toolbar))

    def test_deleting_a_selected_video_keeps_the_other_selection(self):
        toolbar = self.find("AppWindow::selection-toolbar")
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        self.assertIn("2 videos selected", self.labels(toolbar))
        self.press_row_action("Delete ShadowPlay_Clip_01.mp4")
        self.assertIn("1 video selected", self.labels(toolbar))
        rows = [
            entry for entry in self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
            if entry.get("accessibleRole") == "ListItem"
        ]
        self.assertEqual(
            [(entry["accessibleLabel"], entry.get("accessibleItemSelected", False)) for entry in rows],
            [("Ranked_Highlights.mp4", True), ("Friend_Clip.mp4", False)],
        )

    def test_deleting_every_video_reaches_the_empty_state(self):
        list_scroll = self.find("VideoList::list-scroll")
        for _ in range(self.expected_row_count):
            self.call("scroll_element", elementHandle=list_scroll, deltaY=10000, deltaX=0)
            elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
            delete = next(
                entry["accessibleLabel"] for entry in elements
                if entry.get("accessibleLabel", "").startswith("Delete ") and entry.get("accessibleRole") == "Button"
            )
            self.press_row_action(delete)
        self.assertEqual(self.visible_file_names(), [])
        self.assertIn("No videos selected", self.labels())
        elements = self.call("get_element_tree", elementHandle=self.root, maxElements=1000)["elements"]
        compress = next(
            entry for entry in elements
            if entry.get("accessibleLabel") == "Compress" and entry.get("accessibleRole") == "Button"
        )
        self.assertFalse(compress.get("accessibleEnabled", False))

    def test_compress_action_starts_only_that_video(self):
        self.reveal_row(self.rows[0])
        self.press_row_action("Compress ShadowPlay_Clip_01.mp4")
        labels = self.labels(self.rows[0])
        self.assertIn("Compressing · 0%", labels)
        self.assertIn("Cancel ShadowPlay_Clip_01.mp4", labels)
        self.assertNotIn("Compress ShadowPlay_Clip_01.mp4", labels)
        self.assertIn("Compressing · 58%", self.labels(self.rows[1]))
        self.assertIn("Ready to copy", self.labels(self.rows[2]))
        self.assertEqual(self.selected(), [False, False, False])

    def test_cancel_action_returns_that_video_to_waiting(self):
        self.reveal_row(self.rows[1])
        self.press_row_action("Cancel Ranked_Highlights.mp4")
        labels = self.labels(self.rows[1])
        self.assertIn("Waiting for compression", labels)
        self.assertIn("Compress Ranked_Highlights.mp4", labels)
        self.assertNotIn("Cancel Ranked_Highlights.mp4", labels)
        self.assertIn("Waiting for compression", self.labels(self.rows[0]))

    def test_finished_row_offers_copy_instead_of_compress(self):
        self.reveal_row(self.rows[2])
        labels = self.labels(self.rows[2])
        self.assertIn("Copy Friend_Clip.mp4", labels)
        self.assertNotIn("Compress Friend_Clip.mp4", labels)
        self.assertNotIn("Cancel Friend_Clip.mp4", labels)
        self.assertIn("Waiting for compression", self.labels(self.rows[0]))

    def test_selection_toolbar_sits_above_the_videos_not_in_the_settings(self):
        self.click(self.rows[1])
        toolbar = self.find("AppWindow::selection-toolbar")
        bounds = self.call("get_element_properties", elementHandle=toolbar)
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        self.assertAlmostEqual(bounds["absolutePosition"].get("y", 0) + bounds["size"]["height"], videos["absolutePosition"]["y"], delta=0.1)
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

    def test_selection_enables_custom_fields_and_keeps_the_menu_anchored(self):
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
        self.assertEqual(expanded["size"]["width"], initial["size"]["width"])
        self.assertGreater(expanded["size"]["height"], 0)
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
        self.assert_encoding(self.rows[0], "H.264", "1920 × 1080", "16 MB")
        self.assert_encoding(self.rows[1], "H.264", "720p", "20 MB")

    def test_menu_leaves_the_video_list_scrollable(self):
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        self.assertGreater(videos["size"]["height"], 0)
        self.call("scroll_element", elementHandle=self.find("VideoList::list-scroll"), deltaY=-10000, deltaX=0)
        last = self.call("get_element_properties", elementHandle=self.rows[-1])
        self.assertLessEqual(
            last["absolutePosition"]["y"] + last["size"]["height"],
            videos["absolutePosition"]["y"] + videos["size"]["height"],
        )

    def test_long_audio_scrolls_without_resizing_the_menu(self):
        panel = self.find("AppWindow::settings-panel")
        self.click(self.rows[1])
        shown = self.call("get_element_properties", elementHandle=panel)
        content = self.find("SelectionSettings::settings-content")
        before = self.call("get_element_properties", elementHandle=content)
        self.scroll_settings(-10000)
        after = self.call("get_element_properties", elementHandle=content)
        self.assertLess(after["absolutePosition"]["y"], before["absolutePosition"]["y"])
        self.assertEqual(self.call("get_element_properties", elementHandle=panel)["size"], shown["size"])

    def test_row_selection_highlight_fades_in_and_out(self):
        elements = self.call("get_element_tree", elementHandle=self.rows[0], maxElements=100)["elements"]
        highlight = next(
            entry["handle"] for entry in elements
            if any(item.get("id") == "ClipRow::selection-highlight" for item in entry.get("typeNamesAndIds", []))
        )
        self.wait_for_opacity(highlight, 0)
        self.click(self.rows[0])
        entering = self.wait_for_partial_opacity(highlight)
        self.assertGreater(entering, 0)
        self.assertLess(entering, 1)
        self.assertEqual(self.selected(), [True, False, False])
        self.wait_for_opacity(highlight, 1)
        self.click(self.rows[0], control=True)
        leaving = self.wait_for_partial_opacity(highlight)
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

    def sample_selection(self, observed_counts):
        labels = self.labels(self.find("AppWindow::selection-toolbar"))
        for count in [1, 2, 3]:
            label = "1 video selected" if count == 1 else f"{count} videos selected"
            if label in labels:
                observed_counts.add(count)

    def test_settings_follow_selection_before_box_drag_finishes(self):
        self.assertNotIn("1 video selected", self.labels())
        observed_counts = set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            drag = executor.submit(self.box, target_row=self.rows[0])
            while not drag.done():
                self.sample_selection(observed_counts)
                time.sleep(0.02)
            drag.result()
        self.sample_selection(observed_counts)
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
        self.assert_encoding(self.rows[0], "AV1", "720p", "12 MB")
        self.assert_encoding(self.rows[1], "H.264", "720p", "20 MB")
        self.assert_encoding(self.rows[2], "AV1", "720p", "12 MB")
        self.click(self.rows[1])
        self.click(self.rows[2], control=True)
        self.assertEqual(self.checked_options("SelectionSettings::codec-bar"), [])
        self.assertEqual(self.checked_options("SelectionSettings::resolution-bar"), ["720p"])
        self.choose("SelectionSettings::resolution-bar", "1080p")
        self.assert_encoding(self.rows[0], "AV1", "720p", "12 MB")
        self.assert_encoding(self.rows[1], "H.264", "1080p", "20 MB")
        self.assert_encoding(self.rows[2], "AV1", "1080p", "12 MB")

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
        self.assert_encoding(self.rows[0], "AV1", "1080p", "25 MB")
        self.assert_encoding(self.rows[1], "H.264", "720p", "20 MB")
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
        pill, selected = self.codec_highlight()
        self.wait_for_opacity(pill["handle"], 1)
        self.choose("SelectionSettings::codec-bar", "Quality (AV1)")
        pill, selected = self.codec_highlight()
        self.assertIsNotNone(selected)
        self.assertGreater(abs(self.horizontal_center(pill) - self.horizontal_center(selected)), 1)
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
        self.assert_encoding(self.rows[0], "H.264", "1600 × 720", "12 MB")
        self.assert_encoding(self.rows[1], "H.264", "1600 × 1080", "8 MB")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::size-input"), value="16")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::custom-height"), value="900")
        for row in self.rows[:2]:
            self.assert_encoding(row, "H.264", "1600 × 900", "16 MB")

    def test_initial_audio_viewport_matches_the_selected_video(self):
        for index, labels in [
            (0, []),
            (1, ["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio", "Alerts", "Voice chat", "Sound effects"]),
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
        audio = self.find("SelectionSettings::audio-list")
        empty = self.call("get_element_properties", elementHandle=audio)
        self.assertGreater(empty["size"]["height"], 0)
        self.assertEqual(self.audio_tracks(), [])
        self.click(self.rows[0])
        self.assertIn("AUDIO", self.labels())
        self.assertEqual(self.audio_tracks(), [])
        self.assertEqual(self.call("get_element_properties", elementHandle=audio)["size"], empty["size"])
        self.click(self.rows[1])
        self.assertTrue(self.audio_tracks())
        self.assertGreater(self.call("get_element_properties", elementHandle=audio)["size"]["height"], empty["size"]["height"])
        self.click(self.find("AppWindow::clear-touch"))
        self.assertIn("AUDIO", self.labels())
        self.assert_no_segmented_selection()

    def test_demo_audio_scrolls_with_the_menu(self):
        self.click(self.rows[1])
        self.assert_audio_menu_scrolling(["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio", "Alerts", "Voice chat", "Sound effects"])
        commentary = self.audio_track("Commentary")
        self.click(commentary["handle"])
        self.assertTrue(self.audio_track("Commentary")["accessibleChecked"])

    def test_custom_fields_do_not_reduce_the_five_row_audio_minimum(self):
        self.click(self.rows[0])
        audio = self.find("SelectionSettings::audio-list")
        for bar in ["frame-rate-bar", "size-bar", "resolution-bar"]:
            self.choose(f"SelectionSettings::{bar}", "Custom")
            size = self.call("get_element_properties", elementHandle=audio)["size"]
            self.assertAlmostEqual(size["height"], 5 * AUDIO_ROW_HEIGHT + 4, delta=0.1)

    def test_menu_scroll_reaches_the_last_audio_track_with_bottom_padding(self):
        self.click(self.rows[1])
        self.scroll_settings(-10000)
        audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-list"))
        settings = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        bottom = settings["absolutePosition"]["y"] + settings["size"]["height"]
        self.assertAlmostEqual(bottom - audio["absolutePosition"]["y"] - audio["size"]["height"], 32, delta=0.5)
        tracks = [control for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]
        self.assertTrue(tracks)
        self.assertEqual(tracks[-1]["accessibleLabel"], "Sound effects")
        for track in tracks:
            self.assertAlmostEqual(track["size"]["height"], AUDIO_ROW_HEIGHT, delta=0.1)

    def test_mixed_audio_only_compares_videos_that_have_the_track(self):
        self.click(self.rows[2])
        self.click(self.audio_track("Microphone")["handle"])
        self.click(self.rows[0], control=True)
        track = self.audio_track("Microphone")
        self.assertFalse(track.get("accessibleChecked", False))
        self.assertNotEqual(track.get("accessibleDescription"), "Mixed values")
        self.click(self.rows[1], control=True)
        track = self.audio_track("Microphone")
        self.assertFalse(track.get("accessibleChecked", False))
        self.assertEqual(track.get("accessibleDescription"), "Mixed values")
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
        environment.update(DISPLAY=self.display, SLINT_BACKEND="winit-software", SLINT_MCP_PORT=str(port), SLINT_STYLE="cosmic-dark")
        return environment

    def setUp(self):
        if shutil.which("slint-viewer") is None or shutil.which("Xvfb") is None:
            self.skipTest("Needs slint-viewer and Xvfb to load fixture data")
        display, self.display = start_virtual_display()
        self.addCleanup(stop_virtual_display, display)
        super().setUp()


class AudioOverflowTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-overflow.json"

    def test_audio_area_holds_every_track_inside_a_fixed_five_row_menu(self):
        heights = []
        for index, count in [(0, 0), (2, 1), (1, 9)]:
            with self.subTest(track_count=count):
                self.click(self.rows[index])
                self.scroll_settings(-10000)
                audio = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-list"))
                expected_rows = max(5, count)
                self.assertAlmostEqual(
                    audio["size"]["height"], expected_rows * AUDIO_ROW_HEIGHT + expected_rows - 1, delta=0.1,
                )
                heights.append(
                    self.call("get_element_properties", elementHandle=self.find("AppWindow::settings-panel"))["size"]["height"]
                )
        self.assertAlmostEqual(max(heights), min(heights), delta=1)

    def test_menu_setup_scroll_reaches_both_ends_and_clamps(self):
        self.click(self.rows[1])
        viewport = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        content = self.find("SelectionSettings::settings-content")
        menu = self.call("get_element_properties", elementHandle=content)
        self.assertGreater(
            menu["size"]["height"], viewport["size"]["height"],
            "The fixture must make the menu taller than its viewport",
        )
        self.scroll_settings(10000)
        top_aligned = self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"]
        self.assertAlmostEqual(top_aligned, viewport["absolutePosition"]["y"], delta=0.5)
        self.scroll_settings(-10000)
        bottom_aligned = self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"]
        self.assertLess(bottom_aligned, top_aligned)
        self.assertAlmostEqual(
            bottom_aligned,
            viewport["absolutePosition"]["y"] + viewport["size"]["height"] - menu["size"]["height"],
            delta=1,
        )
        self.scroll_settings(10000)
        restored = self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"]
        self.assertAlmostEqual(restored, top_aligned, delta=0.5)
        self.scroll_settings(10000)
        self.assertAlmostEqual(
            self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"], restored, delta=0.5,
        )

    def test_wheel_over_audio_scrolls_the_menu_not_the_tracks_within_the_section(self):
        self.click(self.rows[1])
        self.scroll_settings(10000)
        audio_handle = self.find("SelectionSettings::audio-list")
        rows_handle = self.find("SelectionSettings::audio-rows")
        content_handle = self.find("SelectionSettings::settings-content")
        audio = self.call("get_element_properties", elementHandle=audio_handle)
        rows = self.call("get_element_properties", elementHandle=rows_handle)
        content = self.call("get_element_properties", elementHandle=content_handle)
        viewport = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))
        top = max(audio["absolutePosition"]["y"], viewport["absolutePosition"]["y"])
        bottom = min(
            audio["absolutePosition"]["y"] + audio["size"]["height"],
            viewport["absolutePosition"]["y"] + viewport["size"]["height"],
        )
        self.assertGreater(bottom, top)
        self.call(
            "dispatch_pointer_scroll", windowHandle=self.window,
            position={"x": audio["absolutePosition"]["x"] + audio["size"]["width"] / 2, "y": (top + bottom) / 2},
            deltaY=-10000, deltaX=0,
        )
        audio_after = self.call("get_element_properties", elementHandle=audio_handle)
        rows_after = self.call("get_element_properties", elementHandle=rows_handle)
        content_after = self.call("get_element_properties", elementHandle=content_handle)
        self.assertLess(content_after["absolutePosition"]["y"], content["absolutePosition"]["y"])
        self.assertLess(audio_after["absolutePosition"]["y"], audio["absolutePosition"]["y"])
        self.assertAlmostEqual(
            rows_after["absolutePosition"]["y"] - audio_after["absolutePosition"]["y"],
            rows["absolutePosition"]["y"] - audio["absolutePosition"]["y"], delta=0.1,
        )
        tracks = [element for element in self.menu_controls() if element.get("accessibleRole") == "Checkbox"]
        self.assertEqual(tracks[-1]["accessibleLabel"], "Sound effects")

    def test_toggling_partly_visible_tracks_preserves_menu_scroll_position(self):
        self.click(self.rows[1])
        window = self.call("get_element_properties", elementHandle=self.root)
        self.call(
            "drag_element", elementHandle=self.find("AppWindow::settings-resize"),
            target={"x": window["size"]["width"] / 2, "y": window["size"]["height"] - 5 * AUDIO_ROW_HEIGHT},
        )
        scroll = self.find("SelectionSettings::settings-scroll")
        for offset in [0, AUDIO_ROW_HEIGHT / 3]:
            with self.subTest(scroll_offset=offset):
                self.audio_tracks()
                self.scroll_settings(-offset)
                viewport = self.call("get_element_properties", elementHandle=scroll)
                top = viewport["absolutePosition"]["y"]
                bottom = top + viewport["size"]["height"]
                tracks = [element for element in self.menu_controls() if element.get("accessibleRole") == "Checkbox"]
                clipped = [
                    track for track in tracks
                    if top < track["absolutePosition"]["y"] + track["size"]["height"] / 2 < bottom
                    and (track["absolutePosition"]["y"] < top
                        or track["absolutePosition"]["y"] + track["size"]["height"] > bottom)
                ]
                self.assertTrue(clipped, "The fixture must expose a clickable, partly clipped audio row")
                track = clipped[0]
                rows = self.find("SelectionSettings::audio-rows")
                settings = self.find("SelectionSettings::settings-content")
                rows_before = self.call("get_element_properties", elementHandle=rows)["absolutePosition"]["y"]
                settings_before = self.call("get_element_properties", elementHandle=settings)["absolutePosition"]["y"]
                self.click(track["handle"])
                changed = self.call("get_element_properties", elementHandle=track["handle"])
                self.assertNotEqual(changed.get("accessibleChecked", False), track.get("accessibleChecked", False))
                self.assertAlmostEqual(
                    self.call("get_element_properties", elementHandle=rows)["absolutePosition"]["y"],
                    rows_before, delta=0.1,
                )
                self.assertAlmostEqual(
                    self.call("get_element_properties", elementHandle=settings)["absolutePosition"]["y"],
                    settings_before, delta=0.1,
                )

    def test_menu_scrolls_and_edits_audio_beyond_five_tracks(self):
        self.click(self.rows[1])
        self.assert_audio_menu_scrolling(["Game audio", "Microphone", "Party chat", "Music", "Commentary", "System audio", "Alerts", "Voice chat", "Sound effects"])
        commentary = self.audio_track("Commentary")
        self.assertFalse(commentary.get("accessibleChecked", False))
        self.click(commentary["handle"])
        self.assertTrue(self.audio_track("Commentary")["accessibleChecked"])
        self.click(self.rows[2])
        self.assertEqual([track["accessibleLabel"] for track in self.audio_tracks()], ["Game audio"])

    def rail_thumb(self, rail_id):
        elements = self.call("get_element_tree", elementHandle=self.find(rail_id), maxElements=100)["elements"]
        return next(
            element for element in elements
            if any(item.get("id") == "ScrollRail::thumb" for item in element.get("typeNamesAndIds", []))
        )

    def drag_rail_to_end(self, rail_id):
        rail = self.call("get_element_properties", elementHandle=self.find(rail_id))
        self.call(
            "drag_element", elementHandle=self.rail_thumb(rail_id)["handle"],
            target={
                "x": rail["absolutePosition"]["x"] + rail["size"]["width"] / 2,
                "y": rail["absolutePosition"]["y"] + rail["size"]["height"] - 1,
            },
        )

    def assert_revealed(self, element, viewport):
        bottom = viewport["absolutePosition"]["y"] + viewport["size"]["height"]
        self.assertLessEqual(
            element["absolutePosition"]["y"] + element["size"]["height"], bottom + 0.5,
            f"{element.get('accessibleLabel')} stays below the viewport",
        )

    def audio_labels(self):
        return [control["accessibleLabel"] for control in self.menu_controls() if control.get("accessibleRole") == "Checkbox"]

    def test_settings_rail_drags_the_menu_to_the_last_audio_track(self):
        self.click(self.rows[1])
        self.scroll_settings(10000)
        self.assertNotIn("Sound effects", self.audio_labels())
        self.drag_rail_to_end("SelectionSettings::settings-rail")
        self.assertEqual(self.audio_labels()[-1], "Sound effects")
        self.assert_revealed(
            self.audio_track("Sound effects"),
            self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll")),
        )

    def test_video_rail_drags_the_list_without_changing_selection(self):
        self.click(self.rows[0])
        selected = self.selected()
        last = self.call("get_element_properties", elementHandle=self.rows[-1])
        viewport = self.call("get_element_properties", elementHandle=self.find("VideoList::list-scroll"))
        self.assertGreater(last["absolutePosition"]["y"] + last["size"]["height"], viewport["absolutePosition"]["y"] + viewport["size"]["height"])
        self.drag_rail_to_end("VideoList::list-rail")
        self.assert_revealed(
            self.call("get_element_properties", elementHandle=self.rows[-1]),
            self.call("get_element_properties", elementHandle=self.find("VideoList::list-scroll")),
        )
        self.assertEqual(self.selected(), selected)

    def test_wheel_over_the_settings_rail_scrolls_the_menu(self):
        self.click(self.rows[1])
        self.scroll_settings(10000)
        rail = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-rail"))
        content = self.find("SelectionSettings::settings-content")
        before = self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"]
        self.call(
            "dispatch_pointer_scroll", windowHandle=self.window,
            position={
                "x": rail["absolutePosition"]["x"] + rail["size"]["width"] / 2,
                "y": rail["absolutePosition"]["y"] + rail["size"]["height"] / 2,
            },
            deltaY=-10000, deltaX=0,
        )
        self.assertLess(
            self.call("get_element_properties", elementHandle=content)["absolutePosition"]["y"], before,
        )


class ViewerSelectionStateTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-overflow.json"

    test_frame_rate_sits_to_the_right_of_codec = SelectionTests.test_frame_rate_sits_to_the_right_of_codec
    test_frame_rate_edits_persist_per_video_and_only_share_the_edited_field = SelectionTests.test_frame_rate_edits_persist_per_video_and_only_share_the_edited_field
    test_populated_list_never_shows_the_empty_notice = SelectionTests.test_populated_list_never_shows_the_empty_notice
    test_deselection_dims_settings_without_hiding_or_resizing_them = SelectionTests.test_deselection_dims_settings_without_hiding_or_resizing_them
    test_empty_selection_keeps_settings_visible_and_disabled = SelectionTests.test_empty_selection_keeps_settings_visible_and_disabled


class FooterTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-overflow.json"

    def row_elements(self, row):
        self.reveal_row(row)
        return self.call("get_element_tree", elementHandle=row, maxElements=300)["elements"]

    def badges(self, row):
        return [
            element for element in self.row_elements(row)
            if element.get("accessibleLabel", "").startswith("Audio track ")
        ]

    def test_initial_enabled_tracks_do_not_also_say_audio_is_off(self):
        self.assertIn("No audio", self.labels(self.rows[0]))
        self.assertEqual(
            [badge["accessibleLabel"] for badge in self.badges(self.rows[1])],
            ["Audio track 1: Game audio", "Audio track 2: Microphone", "Audio track 8: Voice chat"],
        )
        for row in self.rows[1:]:
            labels = self.labels(row)
            self.assertNotIn("Off", labels)
            self.assertNotIn("No audio", labels)

    def test_audio_badges_share_one_line_when_the_desktop_footer_has_room(self):
        elements = self.row_elements(self.rows[1])
        codec = next(element for element in elements if element.get("accessibleLabel") == "H.264")
        badges = self.badges(self.rows[1])
        self.assertEqual(len(badges), 3)
        centers = [
            element["absolutePosition"]["y"] + element["size"]["height"] / 2
            for element in [codec, *badges]
        ]
        self.assertLess(max(centers) - min(centers), 1)

    def test_clicking_an_audio_badge_still_selects_its_video(self):
        self.click(self.badges(self.rows[1])[0]["handle"])
        self.assertEqual(self.selected(), [False, True, False])
        self.click(self.badges(self.rows[2])[0]["handle"], control=True)
        self.assertEqual(self.selected(), [False, True, True])

    def test_audio_badges_follow_edits_without_changing_other_videos(self):
        self.click(self.rows[1])
        self.click(self.audio_track("Music")["handle"])
        self.assertEqual(
            [badge["accessibleLabel"] for badge in self.badges(self.rows[1])],
            ["Audio track 1: Game audio", "Audio track 2: Microphone", "Audio track 4: Music", "Audio track 8: Voice chat"],
        )
        self.assertEqual([badge["accessibleLabel"] for badge in self.badges(self.rows[2])], ["Audio track 1: Game audio"])
        self.click(self.rows[2])
        self.click(self.audio_track("Game audio")["handle"])
        self.assertEqual(self.badges(self.rows[2]), [])
        self.assertIn("Off", self.labels(self.rows[2]))
        self.click(self.audio_track("Game audio")["handle"])
        self.assertEqual([badge["accessibleLabel"] for badge in self.badges(self.rows[2])], ["Audio track 1: Game audio"])
        self.assertNotIn("Off", self.labels(self.rows[2]))

    def test_footer_shows_per_video_custom_frame_rates(self):
        self.click(self.rows[0])
        self.choose("SelectionSettings::frame-rate-bar", "Custom")
        self.call("set_element_value", elementHandle=self.find("SelectionSettings::frame-rate-input"), value="48")
        self.assert_encoding(self.rows[0], "H.264", "720p", "20 MB", "48 FPS")
        self.assert_encoding(self.rows[1], "H.264", "720p", "20 MB", "60 FPS")
        self.click(self.rows[1], control=True)
        self.choose("SelectionSettings::frame-rate-bar", "30")
        for row in self.rows[:2]:
            self.assert_encoding(row, "H.264", "720p", "20 MB", "30 FPS")
        self.assert_encoding(self.rows[2], "H.264", "720p", "20 MB", "60 FPS")

    def assert_last_audio_track_keeps_row_height(self):
        self.click(self.rows[2])
        tracks = self.audio_tracks()
        self.assertTrue(tracks)
        for track in tracks[:-1]:
            if track.get("accessibleChecked", False):
                self.click(track["handle"])
        self.assertEqual(len(self.badges(self.rows[2])), 1)
        before = self.call("get_element_properties", elementHandle=self.rows[2])["size"]
        self.click(tracks[-1]["handle"])
        off_size = self.call("get_element_properties", elementHandle=self.rows[2])["size"]
        self.assertIn("Off", self.labels(self.rows[2]))
        self.assertEqual(self.badges(self.rows[2]), [])
        self.assertEqual(off_size, before)
        self.click(tracks[-1]["handle"])
        on_size = self.call("get_element_properties", elementHandle=self.rows[2])["size"]
        self.assertEqual(len(self.badges(self.rows[2])), 1)
        self.assertEqual(on_size, before)

    test_last_audio_track_toggle_does_not_resize_the_video_row = assert_last_audio_track_keeps_row_height


class FooterResponsiveTests(ViewerTests):
    def setUp(self):
        super().setUp()
        deadline = time.monotonic() + self.startup_timeout
        while True:
            window = self.call("get_element_properties", elementHandle=self.root)
            if window["size"] == self.expected_size:
                break
            self.assertLess(time.monotonic(), deadline, f"Viewer did not reach {self.expected_size}: {window['size']}")
            time.sleep(0.02)
        self.call(
            "drag_element", elementHandle=self.find("AppWindow::settings-resize"),
            target={"x": window["size"]["width"] / 2, "y": window["size"]["height"] * 0.8},
        )

    def command(self):
        return [
            "slint-viewer", "--auto-reload", "--load-data", str(self.fixture),
            "tests/fixtures/footer-window.slint",
        ]

    row_elements = FooterTests.row_elements
    badges = FooterTests.badges

    def assert_footer_contents_fit(self, row_index=1, expected_badge_count=3):
        self.assert_encoding(self.rows[row_index], "H.264", "720p", "20 MB")
        elements = self.row_elements(self.rows[row_index])
        footer = next(
            element for element in elements
            if any(item.get("typeName") == "EncodingFooter" for item in element.get("typeNamesAndIds", []))
        )
        contents = self.call("get_element_tree", elementHandle=footer["handle"], maxElements=300)["elements"]
        left = footer["absolutePosition"].get("x", 0)
        top = footer["absolutePosition"]["y"]
        right = left + footer["size"]["width"]
        bottom = top + footer["size"]["height"]
        for element in contents:
            if not element.get("accessibleLabel"):
                continue
            with self.subTest(label=element["accessibleLabel"]):
                self.assertGreaterEqual(element["absolutePosition"].get("x", 0), left - 0.5)
                self.assertGreaterEqual(element["absolutePosition"]["y"], top - 0.5)
                self.assertLessEqual(element["absolutePosition"].get("x", 0) + element["size"]["width"], right + 0.5)
                self.assertLessEqual(element["absolutePosition"]["y"] + element["size"]["height"], bottom + 0.5)
        badges = self.badges(self.rows[row_index])
        self.assertEqual(len(badges), expected_badge_count)
        for badge in badges:
            children = self.call("get_element_tree", elementHandle=badge["handle"], maxElements=20)["elements"]
            number = next(element for element in children if element.get("accessibleLabel", "").isdigit())
            self.assertLess(number["size"]["height"], badge["size"]["height"])
            self.assertLess(number["size"]["width"], badge["size"]["width"])


class FooterNarrowTests(FooterResponsiveTests):
    fixture = ROOT / "tests/fixtures/footer-narrow.json"
    expected_size = {"width": 320, "height": 1100}

    test_footer_values_and_audio_numbers_stay_inside_the_card = FooterResponsiveTests.assert_footer_contents_fit
    test_last_audio_track_toggle_does_not_resize_the_video_row = FooterTests.assert_last_audio_track_keeps_row_height


class FooterLargeTextTests(FooterResponsiveTests):
    fixture = ROOT / "tests/fixtures/footer-large-text.json"
    expected_size = {"width": 1141, "height": 1300}

    test_footer_values_and_audio_numbers_stay_inside_the_card = FooterResponsiveTests.assert_footer_contents_fit
    test_last_audio_track_toggle_does_not_resize_the_video_row = FooterTests.assert_last_audio_track_keeps_row_height


class MenuHeightTests(FooterResponsiveTests):
    fixture = ROOT / "tests/fixtures/menu-height.json"
    expected_size = {"width": 1141, "height": 1100}

    def wait_for_reveal(self, reveal_id, fields_id):
        reveal = self.find(f"SelectionSettings::{reveal_id}")
        fields = self.call("get_element_properties", elementHandle=self.find(f"SelectionSettings::{fields_id}"))
        expected_height = fields["size"]["height"]
        self.assertGreater(expected_height, 0)
        deadline = time.monotonic() + 2
        while True:
            height = self.call("get_element_properties", elementHandle=reveal)["size"].get("height", 0)
            if abs(height - expected_height) < 0.1:
                return
            self.assertLess(time.monotonic(), deadline, f"{reveal_id} did not fully open: {height} / {expected_height}")
            time.sleep(0.02)

    def expand_menu(self):
        window = self.call("get_element_properties", elementHandle=self.root)
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        window_limit = (window["size"]["height"] - videos["absolutePosition"]["y"]) * 0.8
        self.call(
            "drag_element", elementHandle=self.find("AppWindow::settings-resize"),
            target={"x": window["size"]["width"] / 2, "y": window["size"]["height"] - window_limit},
        )
        return self.call("get_element_properties", elementHandle=self.find("AppWindow::settings-panel"))

    def assert_menu_matches_content_limit(self):
        window = self.call("get_element_properties", elementHandle=self.root)
        videos = self.call("get_element_properties", elementHandle=self.find("AppWindow::video-list"))
        content = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-content"))
        rows = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-rows"))
        warning = self.call("get_element_properties", elementHandle=self.find("SelectionSettings::audio-warning"))
        panel = self.call("get_element_properties", elementHandle=self.find("AppWindow::settings-panel"))
        window_limit = (window["size"]["height"] - videos["absolutePosition"]["y"]) * 0.8
        extra_rows = max(0.0, rows["size"].get("height", 0) - (5 * AUDIO_ROW_HEIGHT + 4))
        content_limit = content["size"]["height"] + 2 - extra_rows - warning["size"].get("height", 0)
        self.assertAlmostEqual(panel["size"]["height"], min(content_limit, window_limit), delta=1.5)

    def test_resize_maximum_stops_at_five_audio_rows(self):
        heights = []
        for index in [2, 1]:
            self.click(self.rows[index])
            heights.append(self.expand_menu()["size"]["height"])
            self.assert_menu_matches_content_limit()
        self.assertAlmostEqual(heights[1], heights[0], delta=1)
        self.assertGreater(
            self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-content"))["size"]["height"],
            heights[1],
        )
        self.click(self.rows[2])
        self.assert_menu_matches_content_limit()

    def test_resize_maximum_includes_revealed_custom_fields(self):
        self.click(self.rows[2])
        initial = self.expand_menu()
        for bar, reveal, fields in [
            ("frame-rate-bar", "frame-rate-reveal", "frame-rate-fields"),
            ("size-bar", "size-reveal", "size-fields"),
            ("resolution-bar", "resolution-reveal", "resolution-fields"),
        ]:
            self.choose(f"SelectionSettings::{bar}", "Custom")
            self.wait_for_reveal(reveal, fields)
        expanded = self.expand_menu()
        self.assert_menu_matches_content_limit()
        self.assertGreater(expanded["size"]["height"], initial["size"]["height"])


class MenuHeightWindowLimitTests(MenuHeightTests):
    fixture = ROOT / "tests/fixtures/menu-height-window-limit.json"
    expected_size = {"width": 1141, "height": 762}


class FooterAllAudioTracksTests(FooterResponsiveTests):
    fixture = ROOT / "tests/fixtures/footer-all-audio.json"
    expected_size = {"width": 320, "height": 1100}
    expected_row_count = 1

    def test_all_selected_audio_tracks_wrap_without_clipping_the_card(self):
        self.assert_footer_contents_fit(row_index=0, expected_badge_count=9)


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
        self.assertTrue(any(label.startswith("Track names differ between videos") for label in self.labels()))

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


class AudioNameOverflowTests(ViewerTests):
    fixture = ROOT / "tests/fixtures/audio-name-overflow.json"
    expected_row_count = 2

    def track_positions(self):
        return [round(track["absolutePosition"]["y"], 1) for track in self.menu_controls() if track.get("accessibleRole") == "Checkbox"]

    def panel_top(self):
        return self.call("get_element_properties", elementHandle=self.find("AppWindow::settings-panel"))["absolutePosition"]["y"]

    def content_height(self):
        return self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-content"))["size"]["height"]

    def wait_for_content_height(self, predicate):
        deadline = time.monotonic() + 2
        while True:
            height = self.content_height()
            if predicate(height):
                return height
            self.assertLess(time.monotonic(), deadline, f"Content height stayed {height}")
            time.sleep(0.02)

    def test_warning_appearing_keeps_the_audio_rows_in_place(self):
        self.click(self.rows[0])
        self.call("scroll_element", elementHandle=self.find("SelectionSettings::settings-scroll"), deltaY=-10000, deltaX=0)
        self.assertLess(
            self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-content"))["absolutePosition"]["y"],
            self.call("get_element_properties", elementHandle=self.find("SelectionSettings::settings-scroll"))["absolutePosition"]["y"],
        )
        before = self.track_positions()
        panel = self.panel_top()
        shorter = self.content_height()
        self.click(self.rows[1], control=True)
        taller = self.wait_for_content_height(lambda height: height > shorter)
        labels = [track["accessibleLabel"] for track in self.menu_controls() if track.get("accessibleRole") == "Checkbox"]
        self.assertIn("Different audio, track 1", labels)
        self.assertEqual(self.track_positions(), before)
        self.assertAlmostEqual(self.panel_top(), panel, delta=0.5)
        self.click(self.rows[1], control=True)
        self.wait_for_content_height(lambda height: height < taller)
        self.assertNotIn(
            "Different audio, track 1",
            [track["accessibleLabel"] for track in self.menu_controls() if track.get("accessibleRole") == "Checkbox"],
        )
        self.assertEqual(self.track_positions(), before)

    def test_toggling_a_track_leaves_the_menu_where_it_is(self):
        self.click(self.rows[0])
        self.click(self.rows[1], control=True)
        self.wait_for_content_height(lambda height: height > 0)
        self.call("scroll_element", elementHandle=self.find("SelectionSettings::settings-scroll"), deltaY=-45, deltaX=0)
        content = self.find("SelectionSettings::settings-content")
        before = self.call("get_element_properties", elementHandle=content)
        track = next(t for t in self.menu_controls() if t.get("accessibleRole") == "Checkbox" and t["accessibleLabel"] == "Microphone")
        self.call("invoke_accessibility_action", elementHandle=track["handle"], action="Default_")
        deadline = time.monotonic() + 2
        while self.call("get_element_properties", elementHandle=track["handle"]).get("accessibleChecked", False):
            self.assertLess(time.monotonic(), deadline, "The track did not toggle")
            time.sleep(0.02)
        after = self.call("get_element_properties", elementHandle=content)
        self.assertEqual(after["absolutePosition"]["y"], before["absolutePosition"]["y"])
        self.assertEqual(after["size"]["height"], before["size"]["height"])



def stop_virtual_display(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
