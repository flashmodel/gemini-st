import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure GeminiCLI package directory is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock sublime module if running in standalone Python
if "sublime" not in sys.modules:
    mock_sublime = MagicMock()
    mock_sublime.platform.return_value = "osx"
    sys.modules["sublime"] = mock_sublime

if "sublime_plugin" not in sys.modules:
    mock_sublime_plugin = MagicMock()
    sys.modules["sublime_plugin"] = mock_sublime_plugin

from agents.antigravity_client import AntigravityClient, find_antigravity_cli


class TestFindAntigravityCLI(unittest.TestCase):
    def test_finds_on_path_agy(self):
        with patch("shutil.which", side_effect=lambda cmd: "/usr/local/bin/agy" if cmd == "agy" else None):
            self.assertEqual(find_antigravity_cli(), "/usr/local/bin/agy")

    def test_finds_on_path_antigravity(self):
        with patch("shutil.which", side_effect=lambda cmd: "/opt/bin/antigravity" if cmd == "antigravity" else None):
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
        mock_proc.terminate.assert_called_once()

    def test_agent_session_reset_effort_to_none(self):
        client = AntigravityClient(self.callbacks, model="gemini-3.8-flash", effort="high")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        client.process = mock_proc
        client.agent_session_set_effort(None)
        self.assertIsNone(client.current_effort)
        mock_proc.terminate.assert_called_once()
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
        mock_proc.terminate.assert_called_once()

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


if __name__ == "__main__":

    unittest.main()
