import os
import sys
import unittest
import tempfile
import shutil
from unittest.mock import MagicMock, patch

PARENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock sublime and sublime_plugin if not already mocked
mock_sublime = sys.modules.get("sublime") or MagicMock()
mock_sublime.platform.return_value = "osx"
mock_sublime.INHIBIT_WORD_COMPLETIONS = 8
mock_sublime.INHIBIT_EXPLICIT_COMPLETIONS = 16
mock_sublime.DYNAMIC_COMPLETIONS = 32
mock_sublime.KIND_NAMESPACE = (1, "n", "namespace")
mock_sublime.KIND_VARIABLE = (2, "v", "variable")
mock_sublime.KIND_AMBIGUOUS = (0, "a", "ambiguous")


class MockRegion:
    def __init__(self, a, b=None):
        if b is None:
            b = a
        self.a = a
        self.b = b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def empty(self):
        return self.a == self.b


mock_sublime.Region = MockRegion


class MockCompletionItem:
    def __init__(self, trigger, annotation="", completion="", kind=None):
        self.trigger = trigger
        self.annotation = annotation
        self.completion = completion
        self.kind = kind

    def __repr__(self):
        return f"CompletionItem({self.trigger!r}, completion={self.completion!r})"


mock_sublime.CompletionItem = MockCompletionItem


class MockCompletionList:
    def __init__(self, completions, flags=0):
        self.completions = completions
        self.flags = flags


mock_sublime.CompletionList = MockCompletionList


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

mock_sublime_plugin = sys.modules.get("sublime_plugin") or MagicMock()
mock_sublime_plugin.WindowCommand = MockWindowCommand
mock_sublime_plugin.TextCommand = MockTextCommand
mock_sublime_plugin.EventListener = MockEventListener

sys.modules["sublime_plugin"] = mock_sublime_plugin

from GeminiCLI.chat import (
    AutoComplete,
    AtQuery,
    parse_at_query_text,
    DEFAULT_IGNORED_NAMES,
)
from GeminiCLI.gemini_cli import (
    GeminiChatViewListener,
    GEMINI_CHAT_VIEW,
    GEMINI_ACTIVE_WORKSPACE,
    GEMINI_INPUT_START,
)
from GeminiCLI import gemini_cli


class TestAutocompleteParser(unittest.TestCase):

    def test_parse_at_query_simple(self):
        parsed = parse_at_query_text("Please check @")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "")
        self.assertEqual(dir_part, "")
        self.assertEqual(file_filter, "")
        self.assertEqual(offset, 13)

    def test_parse_at_query_directory(self):
        parsed = parse_at_query_text("Please check @src/")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/")
        self.assertEqual(dir_part, "src/")
        self.assertEqual(file_filter, "")

    def test_parse_at_query_nested_with_filter(self):
        parsed = parse_at_query_text("Check @src/components/Cha")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, "src/components/Cha")
        self.assertEqual(dir_part, "src/components/")
        self.assertEqual(file_filter, "Cha")

    def test_parse_at_query_space_breaks_token(self):
        parsed = parse_at_query_text("Check @src/components something ")
        self.assertIsNone(parsed)

    def test_parse_at_query_escaped_space(self):
        parsed = parse_at_query_text(r"Check @my\ folder/comp")
        self.assertIsNotNone(parsed)
        full_query, dir_part, file_filter, offset = parsed
        self.assertEqual(full_query, r"my\ folder/comp")
        self.assertEqual(dir_part, r"my\ folder/")
        self.assertEqual(file_filter, "comp")


class TestAutocompleteWorkspaceAndRouting(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cwd_dir = os.path.join(self.temp_dir, "frontend")
        self.other_ws_dir = os.path.join(self.temp_dir, "backend")
        os.makedirs(os.path.join(self.cwd_dir, "src", "components"), exist_ok=True)
        os.makedirs(os.path.join(self.other_ws_dir, "cmd", "server"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multi_workspace_target_dir_routing(self):
        other_workspaces = [self.other_ws_dir]

        # 1. CWD root
        resolved_cwd_root = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "")
        self.assertEqual(resolved_cwd_root, self.cwd_dir)

        # 2. CWD subdirectory (e.g. "@src/components/")
        resolved_cwd_sub = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "src/components/")
        self.assertEqual(resolved_cwd_sub, os.path.join(self.cwd_dir, "src", "components"))

        # 3. Other workspace root (e.g. "@backend/")
        resolved_other_root = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "backend/")
        self.assertEqual(resolved_other_root, self.other_ws_dir)

        # 4. Other workspace subdirectory (e.g. "@backend/cmd/server/")
        resolved_other_sub = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "backend/cmd/server/")
        self.assertEqual(resolved_other_sub, os.path.join(self.other_ws_dir, "cmd", "server"))

        # 5. Sandbox boundary protection (traversal outside project)
        invalid_traversal = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "../../etc/")
        self.assertIsNone(invalid_traversal)

        invalid_other_traversal = AutoComplete.resolve_target_dir(self.cwd_dir, other_workspaces, "backend/../../etc/")
        self.assertIsNone(invalid_other_traversal)

    def test_get_workspace_info(self):
        window = MagicMock()
        window_settings = MockSettings()
        window.settings.return_value = window_settings

        # 1. Default window folders
        window.folders.return_value = [self.cwd_dir, self.other_ws_dir]
        active_cwd, other_workspaces = AutoComplete.get_workspace_info(window, "gemini_active_workspace")
        self.assertEqual(active_cwd, self.cwd_dir)
        self.assertEqual(other_workspaces, [self.other_ws_dir])

        # 2. Custom workspace override via window settings
        window_settings.set("gemini_active_workspace", self.other_ws_dir)
        active_cwd, other_workspaces = AutoComplete.get_workspace_info(window, "gemini_active_workspace")
        self.assertEqual(active_cwd, self.other_ws_dir)
        self.assertEqual(other_workspaces, [self.cwd_dir])

        # 3. No window or empty folders
        active_cwd, other_workspaces = AutoComplete.get_workspace_info(None)
        self.assertIsNone(active_cwd)
        self.assertEqual(other_workspaces, [])


class TestAutocompleteScanningAndGeneration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.temp_dir, "components"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir, "hooks"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir, "node_modules"), exist_ok=True)  # ignored
        os.makedirs(os.path.join(self.temp_dir, ".git"), exist_ok=True)          # ignored

        with open(os.path.join(self.temp_dir, "index.ts"), "w") as f:
            f.write("")
        with open(os.path.join(self.temp_dir, "main.ts"), "w") as f:
            f.write("")
        with open(os.path.join(self.temp_dir, ".DS_Store"), "w") as f:           # ignored
            f.write("")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_directory_and_sorting(self):
        sub_dirs, sub_files = AutoComplete.scan_directory(self.temp_dir, "")
        self.assertEqual(sub_dirs, ["components", "hooks"])
        self.assertEqual(sub_files, ["index.ts", "main.ts"])

        # Filtered scan
        sub_dirs_f, sub_files_f = AutoComplete.scan_directory(self.temp_dir, "co")
        self.assertEqual(sub_dirs_f, ["components"])
        self.assertEqual(sub_files_f, [])

    def test_extract_at_query_from_view(self):
        view = MagicMock()
        text = "Hello @comp"
        editable_start = 0

        view.line.return_value = MockRegion(0, len(text))
        view.substr.side_effect = lambda reg: text[reg.begin():reg.end()]

        query = AutoComplete.extract_at_query(view, len(text), editable_start)
        self.assertIsNotNone(query)
        self.assertEqual(query.full_query, "comp")
        self.assertEqual(query.dir_part, "")
        self.assertEqual(query.file_filter, "comp")
        self.assertEqual(query.at_pos, 6)

        # Before editable start
        query_invalid = AutoComplete.extract_at_query(view, 0, editable_start=5)
        self.assertIsNone(query_invalid)

    def test_generate_completions_root_level(self):
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window
        window_settings = MockSettings()
        window.settings.return_value = window_settings
        window.folders.return_value = [self.temp_dir]
        window.views.return_value = []

        text = "Check @"
        view.line.return_value = MockRegion(0, len(text))
        view.substr.side_effect = lambda reg: text[reg.begin():reg.end()]

        comp_list = AutoComplete.generate_completions(
            view=view,
            locations=[len(text)],
            editable_start=0,
            chat_view_flag=GEMINI_CHAT_VIEW,
            chat_workspace_key=GEMINI_ACTIVE_WORKSPACE,
        )

        self.assertIsNotNone(comp_list)
        triggers = [item.trigger for item in comp_list.completions]
        # Directories with trailing slash
        self.assertIn("components/", triggers)
        self.assertIn("hooks/", triggers)
        # Files without trailing slash
        self.assertIn("index.ts", triggers)
        self.assertIn("main.ts", triggers)
        # Ignored files excluded
        self.assertNotIn(".git/", triggers)
        self.assertNotIn("node_modules/", triggers)
        self.assertNotIn(".DS_Store", triggers)

    def test_generate_completions_subdirectory_level(self):
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window
        window_settings = MockSettings()
        window.settings.return_value = window_settings
        window.folders.return_value = [self.temp_dir]

        # Put a file in components
        with open(os.path.join(self.temp_dir, "components", "Button.tsx"), "w") as f:
            f.write("")

        text = "Check @components/"
        view.line.return_value = MockRegion(0, len(text))
        view.substr.side_effect = lambda reg: text[reg.begin():reg.end()]

        comp_list = AutoComplete.generate_completions(
            view=view,
            locations=[len(text)],
            editable_start=0,
            chat_view_flag=GEMINI_CHAT_VIEW,
            chat_workspace_key=GEMINI_ACTIVE_WORKSPACE,
        )

        self.assertIsNotNone(comp_list)
        triggers = [item.trigger for item in comp_list.completions]
        self.assertIn("Button.tsx", triggers)

    def test_check_cascade_trigger(self):
        view = MagicMock()
        sel_mock = MagicMock()
        sel_mock.b = 10
        view.sel.return_value = [sel_mock]

        text = "Check @src/"
        view.line.return_value = MockRegion(0, len(text))
        # pos - 1 is index 9 which is '/'
        view.substr.side_effect = lambda reg: '/' if isinstance(reg, int) and reg == 9 else (
            text[reg.begin():reg.end()] if hasattr(reg, "begin") else text[reg]
        )

        with patch.object(mock_sublime, "set_timeout") as mock_timeout:
            AutoComplete.check_cascade_trigger(view, editable_start=0)
            mock_timeout.assert_called_once()


class TestListenerAutocompleteIntegration(unittest.TestCase):

    def test_listener_on_query_completions(self):
        listener = GeminiChatViewListener()
        view = MagicMock()
        view_settings = MockSettings()
        view_settings.set(GEMINI_CHAT_VIEW, True)
        view_settings.set(GEMINI_INPUT_START, 0)
        view.settings.return_value = view_settings
        view.size.return_value = 10

        window = MagicMock()
        view.window.return_value = window
        window_settings = MockSettings()
        window.settings.return_value = window_settings
        window.folders.return_value = ["/tmp"]
        window.views.return_value = []

        with patch.object(gemini_cli.AutoComplete, "generate_completions", return_value="mock_comp") as mock_gen:
            res = listener.on_query_completions(view, "", [4])
            mock_gen.assert_called_once()
            self.assertEqual(res, "mock_comp")

    def test_listener_on_modified_async(self):
        listener = GeminiChatViewListener()
        view = MagicMock()
        view_settings = MockSettings()
        view_settings.set(GEMINI_CHAT_VIEW, True)
        view_settings.set(GEMINI_INPUT_START, 0)
        view.settings.return_value = view_settings
        view.size.return_value = 10

        sel_mock = MagicMock()
        sel_mock.begin.return_value = 5
        sel_mock.b = 5
        view.sel.return_value = [sel_mock]

        # 1. Typing '@' triggers auto_complete command
        view.substr.return_value = '@'
        listener.on_modified_async(view)
        view.run_command.assert_called_with("auto_complete", {
            "disable_auto_insert": True,
            "api_completions_only": True,
            "next_completion_if_showing": False,
        })

        # 2. Typing '/' triggers cascade check
        view.substr.return_value = '/'
        with patch.object(gemini_cli.AutoComplete, "check_cascade_trigger") as mock_cascade:
            listener.on_modified_async(view)
            mock_cascade.assert_called_once()


if __name__ == "__main__":
    unittest.main()
