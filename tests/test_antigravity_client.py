import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import types

PARENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
# Ensure GeminiCLI package directory is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if "GeminiCLI" not in sys.modules:
    gemini_cli_pkg = types.ModuleType("GeminiCLI")
    gemini_cli_pkg.__path__ = [BASE_DIR]
    sys.modules["GeminiCLI"] = gemini_cli_pkg

# Mock sublime module if running in standalone Python
if "sublime" not in sys.modules:
    mock_sublime = MagicMock()
    mock_sublime.platform.return_value = "osx"
    sys.modules["sublime"] = mock_sublime

if "sublime_plugin" not in sys.modules:
    mock_sublime_plugin = MagicMock()
    class _DummyInputHandler:
        pass
    mock_sublime_plugin.ListInputHandler = _DummyInputHandler
    mock_sublime_plugin.TextInputHandler = _DummyInputHandler
    sys.modules["sublime_plugin"] = mock_sublime_plugin

from agents.antigravity_client import (
    AntigravityClient,
    find_antigravity_cli,
    list_antigravity_sessions,
    _is_workspace_match,
    _read_transcript_summary_and_mtime,
)
from agents.gemini_client import find_gemini_cli
from GeminiCLI.gemini_cli import (
    get_current_agent,
    GeminiSwitchAgentListHandler,
    AGENT_GEMINI,
    AGENT_ANTIGRAVITY,
)


class TestFindAntigravityCLI(unittest.TestCase):
    def test_finds_on_path_agy(self):
        with patch("shutil.which",
                   side_effect=lambda cmd: "/usr/local/bin/agy" if cmd == "agy" else None):
            self.assertEqual(find_antigravity_cli(), "/usr/local/bin/agy")

    def test_finds_on_path_antigravity(self):
        with patch("shutil.which",
                   side_effect=lambda c: "/opt/bin/antigravity" if c == "antigravity" else None):
            self.assertEqual(find_antigravity_cli(), "/opt/bin/antigravity")

    def test_finds_candidate_path_when_not_on_path(self):
        expected = os.path.expanduser("~/.local/bin/agy")
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", side_effect=lambda p: p == expected), \
             patch("os.access", return_value=True):
            self.assertEqual(find_antigravity_cli(), expected)

    def test_returns_none_when_not_found(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            self.assertIsNone(find_antigravity_cli())


class TestFindGeminiCLI(unittest.TestCase):
    def test_finds_on_path_gemini(self):
        with patch("shutil.which",
                   side_effect=lambda cmd: "/usr/local/bin/gemini" if cmd == "gemini" else None):
            self.assertEqual(find_gemini_cli(), "/usr/local/bin/gemini")

    def test_finds_candidate_path_when_not_on_path(self):
        expected = os.path.expanduser("~/.local/bin/gemini")
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", side_effect=lambda p: p == expected), \
             patch("os.access", return_value=True):
            self.assertEqual(find_gemini_cli(), expected)

    def test_returns_none_when_not_found(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            self.assertIsNone(find_gemini_cli())


class TestGetCurrentAgent(unittest.TestCase):
    def test_returns_setting_override_if_present(self):
        view = MagicMock()
        view.settings().has.side_effect = lambda k: k == "gemini_agent"
        view.settings().get.return_value = "gemini"
        self.assertEqual(get_current_agent(view=view), "gemini")

    def test_auto_detect_fallback(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, d="": ""
        with patch("GeminiCLI.gemini_cli.sublime.load_settings", return_value=mock_settings):
            with patch("GeminiCLI.gemini_cli.find_antigravity_cli", return_value=None), \
                 patch("GeminiCLI.gemini_cli.find_gemini_cli", return_value="/bin/gemini"):
                self.assertEqual(get_current_agent(), AGENT_GEMINI)

            with patch("GeminiCLI.gemini_cli.find_antigravity_cli", return_value="/bin/agy"), \
                 patch("GeminiCLI.gemini_cli.find_gemini_cli", return_value="/bin/gemini"):
                self.assertEqual(get_current_agent(), AGENT_ANTIGRAVITY)


class TestGeminiSwitchAgentListHandler(unittest.TestCase):
    def test_order_and_status_when_only_gemini_installed(self):
        with patch("GeminiCLI.gemini_cli.find_antigravity_cli", return_value=None), \
             patch("GeminiCLI.gemini_cli.find_gemini_cli", return_value="/bin/gemini"):
            handler = GeminiSwitchAgentListHandler(current_agent=None)
            items = handler.list_items()
            self.assertEqual(items[0], ("Gemini CLI (gemini) [Installed]", AGENT_GEMINI))
            self.assertEqual(
                items[1], ("Antigravity (antigravity) [Not Installed]", AGENT_ANTIGRAVITY)
            )

    def test_order_and_status_when_both_installed(self):
        with patch("GeminiCLI.gemini_cli.find_antigravity_cli", return_value="/bin/agy"), \
             patch("GeminiCLI.gemini_cli.find_gemini_cli", return_value="/bin/gemini"):
            handler = GeminiSwitchAgentListHandler(current_agent=None)
            items = handler.list_items()
            self.assertEqual(items[0], ("Antigravity (antigravity) [Installed]", AGENT_ANTIGRAVITY))
            self.assertEqual(items[1], ("Gemini CLI (gemini) [Installed]", AGENT_GEMINI))

    def test_respects_current_agent_position(self):
        with patch("GeminiCLI.gemini_cli.find_antigravity_cli", return_value="/bin/agy"), \
             patch("GeminiCLI.gemini_cli.find_gemini_cli", return_value="/bin/gemini"):
            handler = GeminiSwitchAgentListHandler(current_agent=AGENT_GEMINI)
            items = handler.list_items()
            self.assertEqual(items[0][1], AGENT_GEMINI)
            self.assertEqual(items[1][1], AGENT_ANTIGRAVITY)


class TestAntigravityClientBuildCmd(unittest.TestCase):
    def setUp(self):
        self.callbacks = {
            "on_message": MagicMock(),
            "on_user_message": MagicMock(),
            "on_thought": MagicMock(),
            "on_tool_call": MagicMock(),
            "on_error": MagicMock(),
            "on_stop": MagicMock(),
            "on_session_ready": MagicMock(),
            "on_exit": MagicMock(),
        }

    def test_default_flags(self):
        client = AntigravityClient(self.callbacks, cwd="/test/workspace")
        cli, cmd = client._build_cmd(agent_command="/bin/agy")
        self.assertEqual(cli, "/bin/agy")
        self.assertEqual(cmd[0], "/bin/agy")
        self.assertIn("--input-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--mode", cmd)
        self.assertIn("accept-edits", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_model_flash_supplies_required_effort(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-flash")
        _, cmd = client._build_cmd(agent_command="/bin/agy")
        self.assertIn("--model", cmd)
        idx_m = cmd.index("--model")
        self.assertEqual(cmd[idx_m + 1], "gemini-3.8-flash")
        # agy requires --effort for Gemini models, default to medium
        self.assertIn("--effort", cmd)
        idx_e = cmd.index("--effort")
        self.assertEqual(cmd[idx_e + 1], "medium")

        # For pro model, default to high
        client_pro = AntigravityClient(self.callbacks, model="gemini-3.1-pro")
        _, cmd_pro = client_pro._build_cmd(agent_command="/bin/agy")
        self.assertIn("--effort", cmd_pro)
        self.assertEqual(cmd_pro[cmd_pro.index("--effort") + 1], "high")

        # For non-effort models, no --effort is added
        client_claude = AntigravityClient(self.callbacks, model="claude-sonnet-4-6")
        _, cmd_claude = client_claude._build_cmd(agent_command="/bin/agy")
        self.assertNotIn("--effort", cmd_claude)

    def test_explicit_effort_setting(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-pro", effort="low")
        _, cmd = client._build_cmd(agent_command="/bin/agy")
        self.assertIn("--effort", cmd)
        idx_e = cmd.index("--effort")
        self.assertEqual(cmd[idx_e + 1], "low")

        client_high = AntigravityClient(self.callbacks, model="gemini-3.8-pro", effort="high")
        _, cmd_high = client_high._build_cmd(agent_command="/bin/agy")
        self.assertIn("--effort", cmd_high)
        idx_h = cmd_high.index("--effort")
        self.assertEqual(cmd_high[idx_h + 1], "high")

    def test_agent_session_set_effort(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-flash")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        client.process = mock_proc
        client.agent_session_set_effort("high")
        self.assertEqual(client.current_effort, "high")
        self.assertTrue(client._restarting)
        mock_proc.terminate.assert_not_called()

    def test_agent_session_reset_effort_to_none(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-flash", effort="high")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        client.process = mock_proc
        client.agent_session_set_effort(None)
        self.assertIsNone(client.current_effort)
        self.assertTrue(client._restarting)
        mock_proc.terminate.assert_not_called()
        _, cmd = client._build_cmd(agent_command="/bin/agy")
        # Defaults to medium to satisfy agy's requirement
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")

    def test_agent_session_set_model_with_effort(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-flash")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        client.process = mock_proc
        client.agent_session_set_model("gemini-3.8-flash", effort="low")
        self.assertEqual(client.current_model_id, "gemini-3.8-flash")
        self.assertEqual(client.current_effort, "low")
        self.assertTrue(client._restarting)
        mock_proc.terminate.assert_not_called()

    def test_no_model_omits_flag(self):
        client = AntigravityClient(self.callbacks)
        self.assertIsNone(client.current_model_id)
        _, cmd = client._build_cmd(agent_command="/bin/agy")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--effort", cmd)

        client_default = AntigravityClient(self.callbacks, model="default")
        self.assertIsNone(client_default.current_model_id)
        _, cmd_default = client_default._build_cmd(agent_command="/bin/agy")
        self.assertNotIn("--model", cmd_default)
        self.assertNotIn("--effort", cmd_default)

    def test_conversation_flag(self):
        client = AntigravityClient(self.callbacks, session_id="conv-12345")
        _, cmd = client._build_cmd(agent_command="/bin/agy")
        self.assertIn("--conversation", cmd)
        idx_c = cmd.index("--conversation")
        self.assertEqual(cmd[idx_c + 1], "conv-12345")

    def test_workspace_dirs(self):
        client = AntigravityClient(self.callbacks, cwd="/workspace/main")
        extra_dir = "/workspace/second"
        with patch("os.path.isdir", return_value=True):
            _, cmd = client._build_cmd(
                agent_command="/bin/agy",
                extra_env={"GEMINI_CLI_IDE_WORKSPACE_PATH": f"/workspace/main{os.pathsep}{extra_dir}"}
            )
            self.assertIn("--add-dir", cmd)
            idx_d = cmd.index("--add-dir")
            self.assertEqual(cmd[idx_d + 1], extra_dir)


class TestAntigravityClientEvents(unittest.TestCase):
    def setUp(self):
        self.callbacks = {
            "on_message": MagicMock(),
            "on_user_message": MagicMock(),
            "on_thought": MagicMock(),
            "on_tool_call": MagicMock(),
            "on_error": MagicMock(),
            "on_stop": MagicMock(),
            "on_session_ready": MagicMock(),
            "on_exit": MagicMock(),
        }
        self.client = AntigravityClient(self.callbacks, cwd="/test/workspace")

    def test_init_event(self):
        event = {
            "event": "init",
            "conversation_id": "session-xyz",
            "init": {"cwd": "/test/workspace"}
        }
        self.client._handle_event(event)
        self.assertEqual(self.client.session_id, "session-xyz")
        self.assertTrue(self.client.inited)
        self.callbacks["on_session_ready"].assert_called_once()

    def test_agent_response_event(self):
        event = {
            "event": "step_update",
            "step_type": "agent_response",
            "text_delta": "Hello from Antigravity!"
        }
        self.client._handle_event(event)
        self.callbacks["on_message"].assert_called_once_with("Hello from Antigravity!")

    def test_thinking_event(self):
        event = {
            "event": "step_update",
            "step_type": "thinking",
            "text_delta": "Reasoning about the problem..."
        }
        self.client._handle_event(event)
        self.callbacks["on_thought"].assert_called_once_with("Reasoning about the problem...")

    def test_tool_event_run_command(self):
        event = {
            "event": "step_update",
            "step_type": "tool",
            "state": "ACTIVE",
            "step_index": 1,
            "tool_call": {
                "id": "tc-1",
                "name": "run_command",
                "args": {"CommandLine": "git status"}
            }
        }
        self.client._handle_event(event)
        self.callbacks["on_tool_call"].assert_called_once()
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "execute")
        self.assertEqual(update["title"], "git status")
        self.assertEqual(update["status"], "in_progress")
        self.assertEqual(update["toolCallId"], "tc-1")

    def test_tool_event_write_to_file(self):
        event = {
            "event": "step_update",
            "step_type": "tool",
            "state": "ACTIVE",
            "step_index": 2,
            "tool_call": {
                "id": "tc-2",
                "name": "write_to_file",
                "args": {"TargetFile": "/path/to/code.py"}
            }
        }
        self.client._handle_event(event)
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "edit")
        self.assertEqual(update["title"], "/path/to/code.py")

    def test_tool_event_read_file_with_path_and_lines(self):
        event = {
            "event": "step_update",
            "step_type": "tool",
            "state": "ACTIVE",
            "step_index": 3,
            "tool_call": {
                "id": "tc-3",
                "name": "read_file",
                "parameters": {
                    "Path": "/path/to/file.py",
                    "StartLine": 30,
                    "EndLine": 120,
                }
            }
        }
        self.client._handle_event(event)
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "read")
        self.assertEqual(update["title"], "/path/to/file.py#L30-L120")

    def test_tool_event_read_file_with_path_and_start_line(self):
        event = {
            "event": "step_update",
            "step_type": "tool",
            "state": "ACTIVE",
            "step_index": 4,
            "tool_call": {
                "id": "tc-4",
                "name": "read_file",
                "parameters": {
                    "Path": "/path/to/file.py",
                    "StartLine": 50,
                }
            }
        }
        self.client._handle_event(event)
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "read")
        self.assertEqual(update["title"], "/path/to/file.py#L50")

    def test_tool_event_edit_file_with_path(self):
        event = {
            "event": "step_update",
            "step_type": "tool",
            "state": "ACTIVE",
            "step_index": 5,
            "tool_call": {
                "id": "tc-5",
                "name": "edit_file",
                "parameters": {"Path": "/path/to/edit.py"}
            }
        }
        self.client._handle_event(event)
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "edit")
        self.assertEqual(update["title"], "/path/to/edit.py")

    def test_tool_event_search_web_from_tool_info(self):
        event = {
            "event": "step_update",
            "step_update": {
                "conversation_id": "f5203366-e014-4736-b9bd-3aae75ce706d",
                "step_index": 127,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "search_web",
                "tool_info": {
                    "name": "search_web",
                    "parameters": {
                        "query": '"unmanaged_packages_ignore" "package control" sublime'
                    }
                }
            }
        }
        self.client._handle_event(event)
        update = self.callbacks["on_tool_call"].call_args[0][0]
        self.assertEqual(update["kind"], "SearchWeb")
        self.assertEqual(
            update["title"],
            '("unmanaged_packages_ignore" "package control" sublime)'
        )
        self.assertEqual(update["status"], "in_progress")

    def test_result_event_success(self):
        self.client.current_msg_id = 42
        event = {
            "event": "result",
            "status": "SUCCESS"
        }
        self.client._handle_event(event)
        self.callbacks["on_stop"].assert_called_once_with(42, "end_turn")
        self.callbacks["on_error"].assert_not_called()

    def test_result_event_error(self):
        self.client.current_msg_id = 42
        event = {
            "event": "result",
            "status": "ERROR",
            "error": "Quota exceeded"
        }
        self.client._handle_event(event)
        self.callbacks["on_error"].assert_called_once_with("Quota exceeded")
        self.callbacks["on_stop"].assert_called_once_with(42, "end_turn")

    def test_result_event_cancelled(self):
        self.client.current_msg_id = 42
        self.client._interrupted = True
        event = {
            "event": "result",
            "status": "ERROR",
            "error": "context canceled"
        }
        self.client._handle_event(event)
        self.callbacks["on_error"].assert_not_called()
        self.callbacks["on_stop"].assert_called_once_with(42, "cancelled")


class TestAntigravityClientMethods(unittest.TestCase):
    def setUp(self):
        self.callbacks = {
            "on_message": MagicMock(),
            "on_user_message": MagicMock(),
            "on_thought": MagicMock(),
            "on_tool_call": MagicMock(),
            "on_error": MagicMock(),
            "on_stop": MagicMock(),
            "on_session_ready": MagicMock(),
            "on_exit": MagicMock(),
        }
        self.client = AntigravityClient(self.callbacks)

    def test_send_input(self):
        msg_id = self.client.send_input("Explain async in Python")
        self.assertEqual(msg_id, 1)
        self.assertFalse(self.client.input_queue.empty())
        item = self.client.input_queue.get()
        self.assertEqual(item, (1, "Explain async in Python"))

    def test_agent_session_set_model(self):
        self.client.agent_session_set_model("gemini-3.8-pro")
        self.assertEqual(self.client.current_model_id, "gemini-3.8-pro")

    def test_send_permission_response(self):
        with patch.object(self.client, "_write_json") as mock_write:
            self.client.send_permission_response("req-1", "allow_once")
            mock_write.assert_called_once()
            payload = mock_write.call_args[0][0]
            self.assertEqual(payload["event"], "approval_response")
            self.assertEqual(payload["request_id"], "req-1")
            self.assertEqual(payload["response"]["behavior"], "allow")

    def test_available_models_initially_empty(self):
        client = AntigravityClient(self.callbacks)
        self.assertEqual(client.available_models, [])

    def test_parse_models_output(self):
        # Test real tab-separated agy models output
        raw_output = """gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""
        parsed = AntigravityClient._parse_models_output(raw_output)
        self.assertEqual(len(parsed), 5)

        model_ids = [m["modelId"] for m in parsed]
        self.assertNotIn("default", model_ids)
        self.assertNotIn("", model_ids)
        self.assertIn("gemini-3.8-flash", model_ids)
        self.assertIn("gemini-3.7-flash", model_ids)
        self.assertIn("claude-sonnet-4-6", model_ids)
        self.assertIn("claude-opus-4-6-thinking", model_ids)
        self.assertIn("gpt-oss-120b-medium", model_ids)

        gemini_flash = next(m for m in parsed if m["modelId"] == "gemini-3.8-flash")
        self.assertTrue(gemini_flash["supportsEffort"])
        self.assertEqual(set(gemini_flash["supportedReasoningEfforts"]), {"high", "medium", "low"})

        claude = next(m for m in parsed if m["modelId"] == "claude-sonnet-4-6")
        self.assertFalse(claude["supportsEffort"])

    def test_effort_only_added_when_model_supports_it(self):
        # Gemini model supports effort -> --effort flag is present
        client_gemini = AntigravityClient(self.callbacks, model="gemini-3.8-flash", effort="high")
        client_gemini.available_models = [
            {"modelId": "gemini-3.8-flash", "supportsEffort": True}
        ]
        _, cmd_gemini = client_gemini._build_cmd(agent_command="/bin/agy")
        self.assertIn("--effort", cmd_gemini)
        idx_e = cmd_gemini.index("--effort")
        self.assertEqual(cmd_gemini[idx_e + 1], "high")

        # Claude model does NOT support effort -> --effort flag is omitted even if effort is set
        client_claude = AntigravityClient(self.callbacks, model="claude-sonnet-4-6", effort="high")
        client_claude.available_models = [
            {"modelId": "claude-sonnet-4-6", "supportsEffort": False}
        ]
        _, cmd_claude = client_claude._build_cmd(agent_command="/bin/agy")
        self.assertIn("--model", cmd_claude)
        self.assertNotIn("--effort", cmd_claude)

        # Default model -> --effort flag is omitted
        client_default = AntigravityClient(self.callbacks, model="default", effort="high")
        _, cmd_default = client_default._build_cmd(agent_command="/bin/agy")
        self.assertNotIn("--model", cmd_default)
        self.assertNotIn("--effort", cmd_default)


class TestWorkspaceMatching(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(_is_workspace_match("/path/to/project", "/path/to/project"))

    def test_cwd_inside_workspace(self):
        self.assertTrue(_is_workspace_match("/path/to", "/path/to/project/sub"))

    def test_workspace_inside_cwd(self):
        self.assertTrue(_is_workspace_match("/path/to/project/sub", "/path/to/project"))

    def test_unrelated_paths(self):
        self.assertFalse(_is_workspace_match("/path/to/projectA", "/path/to/projectB"))

    def test_empty_or_none(self):
        self.assertFalse(_is_workspace_match(None, "/path/to/project"))
        self.assertFalse(_is_workspace_match("/path/to/project", None))
        self.assertFalse(_is_workspace_match("", ""))


class TestListAntigravitySessions(unittest.TestCase):
    @patch("agents.antigravity_client._get_base_dir")
    def test_multi_root_workspace_matching(self, mock_base_dir):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_base_dir.return_value = tmp_dir
            cache_dir = os.path.join(tmp_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            meta_path = os.path.join(cache_dir, "conversation_metadata.json")

            ws_alpha = "/workspace/project-alpha"
            ws_beta = "/workspace/project-beta"
            ws_gamma = "/workspace/project-gamma"
            ws_unrelated = "/workspace/other-project"

            data = {
                "conversations": {
                    "sess-1": {
                        "summary": {
                            "ID": "sess-1",
                            "Title": "Multi Root Project Work",
                            "NumSteps": 5,
                            "UpdatedAt": "2026-10-16T15:00:00Z",
                            "WorkspaceURIs": [
                                f"file://{ws_alpha}",
                                f"file://{ws_beta}",
                                f"file://{ws_gamma}",
                            ],
                        }
                    },
                    "sess-empty": {
                        "summary": {
                            "ID": "sess-empty",
                            "Title": "Empty Session",
                            "NumSteps": 0,
                            "UpdatedAt": "2026-10-16T15:00:00Z",
                            "WorkspaceURIs": [f"file://{ws_beta}"],
                        }
                    },
                }
            }
            with open(meta_path, "w") as f:
                json.dump(data, f)

            # Query with second workspace in list: ws_beta
            results_beta = list_antigravity_sessions(ws_beta)
            self.assertEqual(len(results_beta), 1)
            self.assertEqual(results_beta[0]["session_id"], "sess-1")
            self.assertEqual(results_beta[0]["summary"], "Multi Root Project Work")
            self.assertEqual(results_beta[0]["steps"], 5)

            # Query with third workspace: ws_gamma
            results_gamma = list_antigravity_sessions(ws_gamma)
            self.assertEqual(len(results_gamma), 1)
            self.assertEqual(results_gamma[0]["session_id"], "sess-1")

            # Query with unrelated workspace: should be empty
            results_unrelated = list_antigravity_sessions(ws_unrelated)
            self.assertEqual(len(results_unrelated), 0)

            # Query with None (all): returns sess-1, skips sess-empty (0 steps)
            results_all = list_antigravity_sessions(None)
            self.assertEqual(len(results_all), 1)
            self.assertEqual(results_all[0]["session_id"], "sess-1")

    @patch("agents.antigravity_client._get_base_dir")
    def test_read_transcript_summary_and_mtime_direct(self, mock_base_dir):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_base_dir.return_value = tmp_dir
            cid = "sess-direct-1"

            # When transcript does not exist
            summary, mtime = _read_transcript_summary_and_mtime(cid)
            self.assertEqual(summary, "")
            self.assertEqual(mtime, 0.0)

            # When transcript exists with user input
            log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
            os.makedirs(log_dir, exist_ok=True)
            t_path = os.path.join(log_dir, "transcript_full.jsonl")
            with open(t_path, "w") as f:
                f.write(json.dumps({
                    "type": "USER_INPUT",
                    "content": "Optimize search index query",
                }) + "\n")

            summary, mtime = _read_transcript_summary_and_mtime(cid)
            self.assertEqual(summary, "Optimize search index query")
            self.assertGreater(mtime, 0.0)


if __name__ == "__main__":

    unittest.main()
