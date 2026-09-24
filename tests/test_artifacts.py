import os
import sys
import unittest
import tempfile
import json
import shutil
from unittest.mock import MagicMock, patch

PARENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock sublime and sublime_plugin if not already mocked
mock_sublime = MagicMock()
mock_sublime.platform.return_value = "osx"
mock_sublime.Region = lambda a, b: MagicMock(
    begin=lambda: a,
    end=lambda: b,
    contains=lambda pt: a <= pt <= b,
    empty=lambda: a == b
)
mock_sublime.HIDDEN = 1
mock_sublime.PERSISTENT = 2
mock_sublime.LAYOUT_BLOCK = 1
mock_sublime.LAYOUT_INLINE = 2
mock_sublime.DRAW_NO_FILL = 4
mock_sublime.DRAW_NO_OUTLINE = 8

class MockSettings:
    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, val):
        self._data[key] = val

    def has(self, key):
        return key in self._data

    def erase(self, key):
        self._data.pop(key, None)

_pkg_settings = MockSettings()
mock_sublime.load_settings.return_value = _pkg_settings

sys.modules["sublime"] = mock_sublime

class MockWindowCommand:
    def __init__(self, window):
        self.window = window

class MockTextCommand:
    def __init__(self, view):
        self.view = view

class MockEventListener:
    pass

mock_sublime_plugin = MagicMock()
mock_sublime_plugin.WindowCommand = MockWindowCommand
mock_sublime_plugin.TextCommand = MockTextCommand
mock_sublime_plugin.EventListener = MockEventListener

sys.modules["sublime_plugin"] = mock_sublime_plugin

from GeminiCLI.chat import (
    ArtifactItem,
    extract_title_from_markdown,
    read_artifact_metadata,
    get_brain_dirs,
    is_plan_file,
    is_walkthrough_file,
    classify_artifact,
    is_user_artifact_tool_call,
    open_artifact,
    AntigravityArtifactManager,
)
from GeminiCLI import gemini_cli


class TestArtifactHelpers(unittest.TestCase):

    def test_extract_title_from_markdown(self):
        content = """# My Implementation Plan
Some description here.
## Subheading
"""
        self.assertEqual(extract_title_from_markdown(content), "My Implementation Plan")

        bracketed = """
# [ARTIFACT: Walkthrough]
Step 1: Done.
"""
        self.assertEqual(extract_title_from_markdown(bracketed), "ARTIFACT: Walkthrough")

        empty = "Just text without headings."
        self.assertEqual(extract_title_from_markdown(empty), "")
        self.assertEqual(extract_title_from_markdown(""), "")

    def test_read_artifact_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "test.md")
            meta_path = file_path + ".metadata.json"
            with open(file_path, "w") as f:
                f.write("# Test\n")
            with open(meta_path, "w") as f:
                json.dump({"summary": "A test artifact", "requestFeedback": True}, f)

            meta = read_artifact_metadata(file_path)
            self.assertEqual(meta.get("summary"), "A test artifact")
            self.assertTrue(meta.get("requestFeedback"))

            non_existent = read_artifact_metadata(os.path.join(tmpdir, "none.md"))
            self.assertEqual(non_existent, {})

    def test_is_plan_file(self):
        self.assertTrue(is_plan_file("implementation_plan.md"))
        self.assertTrue(is_plan_file("antigravity_provider_plan.md"))
        self.assertTrue(is_plan_file("/some/path/my_plan.md"))
        self.assertFalse(is_plan_file("plan.txt"))
        self.assertFalse(is_plan_file("walkthrough.md"))
        self.assertFalse(is_plan_file(""))

    def test_is_walkthrough_file(self):
        self.assertTrue(is_walkthrough_file("walkthrough.md"))
        self.assertTrue(is_walkthrough_file("feature_walkthrough.md"))
        self.assertTrue(is_walkthrough_file("/path/to/walkthrough.md"))
        self.assertFalse(is_walkthrough_file("walkthrough.json"))
        self.assertFalse(is_walkthrough_file("implementation_plan.md"))
        self.assertFalse(is_walkthrough_file(""))

    def test_classify_artifact(self):
        kind, label, prio = classify_artifact("implementation_plan.md")
        self.assertEqual(kind, "plan")
        self.assertEqual(label, "Plan")
        self.assertEqual(prio, 0)

        kind, label, prio = classify_artifact("walkthrough.md")
        self.assertEqual(kind, "walkthrough")
        self.assertEqual(label, "Walkthrough")
        self.assertEqual(prio, 1)

        kind, label, prio = classify_artifact("notes.md", {"requestFeedback": True})
        self.assertEqual(kind, "plan")

        kind, label, prio = classify_artifact("notes.md", {})
        self.assertEqual(kind, "artifact")
        self.assertEqual(prio, 2)

    def test_is_user_artifact_tool_call(self):
        self.assertTrue(is_user_artifact_tool_call(
            "write_to_file",
            {"TargetFile": "/path/implementation_plan.md"}
        ))
        self.assertTrue(is_user_artifact_tool_call(
            "replace_file_content",
            {"TargetFile": "/path/walkthrough.md"}
        ))
        self.assertTrue(is_user_artifact_tool_call(
            "write_to_file",
            {"TargetFile": "/path/data.md", "ArtifactMetadata": {"UserFacing": True}}
        ))
        self.assertFalse(is_user_artifact_tool_call(
            "run_command",
            {"CommandLine": "ls"}
        ))
        self.assertFalse(is_user_artifact_tool_call(
            "write_to_file",
            {"TargetFile": "/path/regular_code.py"}
        ))

    def test_get_brain_dirs(self):
        dirs = get_brain_dirs("test-session-123")
        self.assertIsInstance(dirs, list)
        self.assertEqual(get_brain_dirs(""), [])


class TestAntigravityArtifactManager(unittest.TestCase):

    def setUp(self):
        self.view = MagicMock()
        self.view.size.return_value = 100
        self.window = MagicMock()
        self.input_start_fn = lambda v: 100
        self.manager = AntigravityArtifactManager(self.view, self.window, self.input_start_fn)

    def test_record_from_tool_call(self):
        tool_call = {
            "name": "write_to_file",
            "parameters": {
                "TargetFile": "/path/to/antigravity_provider_plan.md",
                "CodeContent": "# Antigravity Plan\nContent here...",
                "ArtifactMetadata": {
                    "Summary": "Plan summary for Antigravity",
                    "RequestFeedback": True
                }
            }
        }
        self.manager.record_from_tool_call(tool_call)
        self.assertIn("/path/to/antigravity_provider_plan.md", self.manager.artifacts)
        item = self.manager.artifacts["/path/to/antigravity_provider_plan.md"]
        self.assertEqual(item.kind, "plan")
        self.assertEqual(item.title, "Plan summary for Antigravity")
        self.assertIn("/path/to/antigravity_provider_plan.md", self.manager.pending_render)

    def test_scan_session_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = os.path.join(tmpdir, "test-conv-id")
            os.makedirs(brain_dir)

            plan_path = os.path.join(brain_dir, "implementation_plan.md")
            with open(plan_path, "w") as f:
                f.write("# Plan Title\nDetails...")
            with open(plan_path + ".metadata.json", "w") as f:
                json.dump({"summary": "Summary from metadata"}, f)

            walk_path = os.path.join(brain_dir, "walkthrough.md")
            with open(walk_path, "w") as f:
                f.write("# Walkthrough Heading\nAll verified.")

            with patch("GeminiCLI.chat.artifacts.get_brain_dirs", return_value=[brain_dir]):
                discovered = self.manager.scan_session_artifacts("test-conv-id")
                self.assertEqual(len(discovered), 2)
                self.assertIn(plan_path, self.manager.artifacts)
                self.assertIn(walk_path, self.manager.artifacts)

                plan_item = self.manager.artifacts[plan_path]
                self.assertEqual(plan_item.kind, "plan")
                self.assertEqual(plan_item.title, "Summary from metadata")

                walk_item = self.manager.artifacts[walk_path]
                self.assertEqual(walk_item.kind, "walkthrough")
                self.assertEqual(walk_item.title, "Walkthrough Heading")

                # Second scan with same mtime should not re-add to pending_render
                self.manager.pending_render.clear()
                rediscovered = self.manager.scan_session_artifacts("test-conv-id")
                self.assertEqual(len(rediscovered), 0)
                self.assertEqual(len(self.manager.pending_render), 0)

    def test_render_pending_artifacts(self):
        plan_item = ArtifactItem(
            path="/tmp/my_plan.md",
            name="my_plan.md",
            kind="plan",
            label="Plan",
            title="My Plan Title"
        )
        walk_item = ArtifactItem(
            path="/tmp/walkthrough.md",
            name="walkthrough.md",
            kind="walkthrough",
            label="Walkthrough",
            title="Verification Walkthrough"
        )
        self.manager.artifacts["/tmp/walkthrough.md"] = walk_item
        self.manager.artifacts["/tmp/my_plan.md"] = plan_item
        self.manager.pending_render = ["/tmp/walkthrough.md", "/tmp/my_plan.md"]

        self.manager.render_pending_artifacts()

        # Check append was called with formatted lines
        self.view.run_command.assert_called_once()
        cmd = self.view.run_command.call_args[0][0]
        args = self.view.run_command.call_args[0][1]
        self.assertEqual(cmd, "gemini_chat_append")
        appended_text = args["text"]
        # Plans must be sorted before walkthroughs
        plan_idx = appended_text.find("▣ my_plan.md")
        walk_idx = appended_text.find("▣ walkthrough.md")
        self.assertTrue(plan_idx >= 0)
        self.assertTrue(walk_idx >= 0)
        self.assertLess(plan_idx, walk_idx)

        self.assertEqual(len(self.manager.rendered_regions), 2)
        self.assertEqual(len(self.manager.pending_render), 0)

    def test_open_artifact_disk_file(self):
        with tempfile.NamedTemporaryFile(suffix=".md") as tmp:
            tmp.write(b"# Temp markdown\n")
            tmp.flush()

            self.manager.open_artifact(tmp.name)
            self.window.open_file.assert_called_with(tmp.name)

    def test_open_artifact_in_memory_content(self):
        mock_new_view = MagicMock()
        self.window.new_file.return_value = mock_new_view
        self.window.views.return_value = []

        item = ArtifactItem(
            path="/non/existent/plan.md",
            name="plan.md",
            kind="plan",
            content="# In-Memory Plan\nBody..."
        )
        self.manager.artifacts[item.path] = item

        self.manager.open_artifact(item.path, item)
        self.window.new_file.assert_called_once()
        mock_new_view.run_command.assert_called_with("append", {"characters": item.content})
        mock_new_view.set_scratch.assert_called_with(True)

    def test_open_artifact_at_region_click(self):
        reg = MagicMock()
        reg.contains.side_effect = lambda pt: 10 <= pt <= 50
        item = ArtifactItem(path="/path/plan.md", name="plan.md", kind="plan")
        self.manager.rendered_regions.append((reg, "/path/plan.md", item))

        with patch.object(self.manager, "open_artifact") as mock_open:
            # Click inside region
            self.assertTrue(self.manager.open_artifact_at(25))
            mock_open.assert_called_with("/path/plan.md", item)

            # Click outside region
            mock_open.reset_mock()
            self.assertFalse(self.manager.open_artifact_at(100))
            mock_open.assert_not_called()

    def test_open_artifact_at_line_text_fallback(self):
        self.view.line.return_value = MagicMock()
        self.view.substr.return_value = "▣ implementation_plan.md"
        item = ArtifactItem(path="/path/implementation_plan.md", name="implementation_plan.md", kind="plan")
        self.manager.artifacts[item.path] = item

        with patch.object(self.manager, "open_artifact") as mock_open:
            self.assertTrue(self.manager.open_artifact_at(5))
            mock_open.assert_called_with("/path/implementation_plan.md", item)

    def test_clear(self):
        self.manager.artifacts["/some/path"] = MagicMock()
        self.manager.pending_render.append("/some/path")
        self.manager.synced_mtimes["/some/path"] = 123.0
        self.manager.clear()
        self.assertEqual(len(self.manager.artifacts), 0)
        self.assertEqual(len(self.manager.pending_render), 0)
        self.assertEqual(len(self.manager.synced_mtimes), 0)
        self.view.erase_regions.assert_called()


class TestArtifactListenerIntegration(unittest.TestCase):

    def test_listener_double_click_opens_artifact(self):
        listener = gemini_cli.GeminiChatViewListener()
        view = MagicMock()
        view.settings().get.side_effect = lambda k, default=None: True if k == "gemini_chat_view" else default
        view.name.return_value = "Gemini Chat"
        view.window_to_text.return_value = 42

        window = MagicMock()
        window.id.return_value = 1001
        view.window.return_value = window

        mock_session = MagicMock()
        mock_manager = MagicMock()
        mock_session.artifact_manager = mock_manager
        mock_manager.open_artifact_at.return_value = True

        with patch.dict(gemini_cli.gemini_clients, {1001: mock_session}):
            with patch.object(gemini_cli, "open_local_file_link", return_value=False):
                res = listener.on_text_command(
                    view,
                    "drag_select",
                    {"by": "words", "event": {"x": 100, "y": 200}}
                )
                self.assertEqual(res, ("noop", {}))
                mock_manager.open_artifact_at.assert_called_once_with(42)


class TestChatSessionArtifactIntegration(unittest.TestCase):

    def setUp(self):
        self.window = MagicMock()
        self.w_settings = MockSettings()
        self.window.settings.return_value = self.w_settings

        self.view = MagicMock()
        self.v_settings = MockSettings()
        self.view.settings.return_value = self.v_settings
        self.view.window.return_value = self.window

    def test_artifact_manager_lifecycle(self):
        from GeminiCLI.gemini_cli import ChatSession, AGENT_ANTIGRAVITY, AGENT_GEMINI
        with patch.object(gemini_cli, "get_best_dir", return_value="/mock/cwd"):
            # 1. Default agent is antigravity: artifact_manager created
            session = ChatSession(self.window, self.view, cwd="/mock/cwd")
            self.assertIsNotNone(session.artifact_manager)
            self.assertEqual(session.artifact_manager.__class__.__name__, "AntigravityArtifactManager")

            # 2. Switch to Gemini: artifact_manager cleared and set to None
            with patch.object(session, "_start_client"):
                session.switch_agent(AGENT_GEMINI)
                self.assertIsNone(session.artifact_manager)

            # 3. Switch back to Antigravity: artifact_manager created
            with patch.object(session, "_start_client"):
                session.switch_agent(AGENT_ANTIGRAVITY)
                self.assertIsNotNone(session.artifact_manager)
                self.assertEqual(session.artifact_manager.__class__.__name__, "AntigravityArtifactManager")

    def test_chat_session_on_tool_call_records_artifact(self):
        from GeminiCLI.gemini_cli import ChatSession, AGENT_ANTIGRAVITY
        self.v_settings.set("gemini_agent", AGENT_ANTIGRAVITY)
        with patch.object(gemini_cli, "get_best_dir", return_value="/mock/cwd"):
            session = ChatSession(self.window, self.view, cwd="/mock/cwd")
            self.assertIsNotNone(session.artifact_manager)

            tool_call = {
                "name": "write_to_file",
                "parameters": {
                    "TargetFile": "/path/to/implementation_plan.md",
                    "CodeContent": "# My Plan\nContent"
                },
                "status": "completed"
            }
            with patch.object(session.artifact_manager, "render_pending_artifacts") as mock_render:
                with patch.object(gemini_cli.sublime, "set_timeout",
                                  side_effect=lambda fn, delay=0: fn() if delay == 0 else None):
                    session.on_tool_call(tool_call)
                    self.assertIn("/path/to/implementation_plan.md", session.artifact_manager.artifacts)
                    mock_render.assert_called_once()

    def test_chat_session_on_stop_syncs_artifacts(self):
        from GeminiCLI.gemini_cli import ChatSession, AGENT_ANTIGRAVITY
        self.v_settings.set("gemini_agent", AGENT_ANTIGRAVITY)
        self.v_settings.set("gemini_session_id", "conv-xyz-123")
        with patch.object(gemini_cli, "get_best_dir", return_value="/mock/cwd"):
            session = ChatSession(self.window, self.view, cwd="/mock/cwd")
            with patch.object(session.artifact_manager, "sync_and_render") as mock_sync:
                with patch.object(gemini_cli.sublime, "set_timeout",
                                  side_effect=lambda fn, delay=0: fn() if delay == 0 else None):
                    session.on_stop(1, "end_turn")
                    mock_sync.assert_called_once_with("conv-xyz-123")


if __name__ == "__main__":
    unittest.main()
