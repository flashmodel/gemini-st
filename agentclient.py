import subprocess
import threading
import queue
import json
import shutil
import sys
import logging
import os
import sublime

LOG = logging.getLogger(__package__)

def _find_gemini_cli():
    """Search common default install locations for the gemini CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(appdata, "npm", "gemini.cmd"),
            os.path.join(appdata, "npm", "gemini"),
            os.path.join(local_appdata, "Programs", "gemini", "gemini.exe"),
            os.path.join(local_appdata, "Programs", "gemini", "gemini.cmd"),
        ]
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "gemini"),
            os.path.join(home, ".npm-global", "bin", "gemini"),
            os.path.join(home, ".yarn", "bin", "gemini"),
            "/usr/local/bin/gemini",
            "/opt/homebrew/bin/gemini",
            "/home/linuxbrew/.linuxbrew/bin/gemini",
        ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            LOG.info(f"Found gemini CLI at default location: {path}")
            return path
    return None


class ErrorCode:
    AuthRequired = -32000
    InternalError = -32603


def _version_greater_or_equal(v1, v2):
    """Simple version comparison (>=)."""
    try:
        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]
        # Pad with zeros
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        return parts1 >= parts2
    except (ValueError, AttributeError):
        return False


class GeminiClient:
    """
    Handles Gemini CLI protocol communication and message processing.
    """
    def __init__(self, callbacks, cwd=None, session_id=None, ignore_history=False):
        self.callbacks = callbacks
        self.cwd = cwd if cwd else os.path.expanduser("~")
        self.process = None
        self.input_queue = queue.Queue()
        self.message_id = 0
        self.session_id = session_id if session_id else ""
        self.inited = False
        self.agent_capabilities = {}
        self.agent_version = "0.0.0"
        self.init_event = threading.Event()
        self.session_event = threading.Event()
        self._pending_requests = {}  # {msg_id: method_name}
        # Used to suppress history stream when reloading an already populated view
        self.ignore_messages = ignore_history

    def _get_acp_flag(self, gemini_command, env):
        """
        Determine the correct ACP flag based on the gemini-cli version.
        Versions prior to 0.34.0 require the --experimental-acp flag.
        Version 0.34.0 and later use the stabilized --acp flag.
        """
        try:
            import re
            v_args = {}
            if sublime.platform() == 'windows':
                v_args['creationflags'] = subprocess.CREATE_NO_WINDOW

            version_out = subprocess.check_output(
                [gemini_command, "--version"],
                env=env,
                universal_newlines=True,
                stderr=subprocess.STDOUT,
                **v_args
            ).strip()
            match = re.search(r'(\d+\.\d+\.\d+)', version_out)
            if match:
                # 0.34.0 stabilizes the ACP protocol and uses --acp
                if not _version_greater_or_equal(match.group(1), "0.34.0"):
                    return "--experimental-acp"
        except Exception as e:
            msg = "Failed to check gemini-cli version: %s" % e
            LOG.error(msg)
        return "--acp"

    def start(self, api_key=None, gemini_command=None, extra_env=None):
        """Start the Gemini CLI process and communication threads."""
        threading.Thread(
            target=self._start_thread,
            args=(api_key, gemini_command, extra_env),
            daemon=True
        ).start()

    def _start_thread(self, api_key, gemini_command, extra_env):
        if not gemini_command:
            gemini_command = shutil.which("gemini") or _find_gemini_cli()

        if not gemini_command:
            self.callbacks['on_error'](
                "Gemini CLI not found. Please install it first:\n"
                "npm install -g @google/gemini-cli"
            )
            return

        try:
            env = os.environ.copy()
            if extra_env:
                env.update(extra_env)

            if api_key:
                env["GOOGLE_API_KEY"] = api_key
                LOG.info("Starting Gemini CLI with custom API key from settings")

            LOG.info("Gemini CLI start cwd=%s", self.cwd)

            # Prepare subprocess arguments
            popen_args = {
                'stdin': subprocess.PIPE,
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'shell': False,
                'env': env,
                'encoding': 'utf-8',
                'universal_newlines': True,
                'bufsize': 1
            }

            # On Windows, prevent console window from appearing
            if sublime.platform() == 'windows':
                popen_args['creationflags'] = subprocess.CREATE_NO_WINDOW

            acp_flag = self._get_acp_flag(gemini_command, env)

            self.process = subprocess.Popen(
                [gemini_command, acp_flag],
                **popen_args
            )

            # Start reader and writer threads
            threading.Thread(target=self._read_loop, daemon=True).start()
            threading.Thread(target=self._write_loop, daemon=True).start()

        except FileNotFoundError:
            self.callbacks['on_error']("gemini-cli command not found")
        except Exception as e:
            self.callbacks['on_error']("gemini-cli exec error: %s" % e)

    def stop(self):
        """Stop the client and terminate the process."""
        self.input_queue.put(None)

    def send_input(self, text):
        """Queue user input to be sent to Gemini."""
        self.ignore_messages = False
        msgid = self._next_message_id()
        self.input_queue.put((msgid, text))
        return msgid

    def send_permission_response(self, msg_id, option_id):
        """Send permission selection response."""
        self._send_response(msg_id, {"outcome": {"outcome": "selected", "optionId": option_id}})

    def _read_loop(self):
        """Read and process messages from Gemini CLI."""
        try:
            for line in iter(self.process.stdout.readline, ""):
                if line:
                    message = json.loads(line.strip())
                    self._handle_message(message)
            LOG.info("gemini stdio closed")
        except Exception as e:
            LOG.error("gemini read stdout error: %s", e)
        finally:
            LOG.info("gemini cli session ended")
            self.callbacks['on_exit']()

    def _handle_message(self, message):
        """Process a single message from Gemini."""
        if "result" in message:
            self._handle_result(message["id"], message["result"])
        elif "error" in message:
            self._handle_error(message)
        elif message.get("method") == "session/update":
            self._handle_session_update(message["params"]["update"])
        elif message.get("method") == "fs/read_text_file":
            self._handle_fs_read(message)
        elif message.get("method") == "fs/write_text_file":
            self._handle_fs_write(message)
        elif message.get("method") == "session/request_permission":
            self._handle_permission_request(message)
        else:
            LOG.info("unprocessed message: %s" % message)

    def _handle_error(self, message):
        """Handle error messages."""
        error = message.get("error", {})
        err_msg = error.get("message", "Internal error")
        msg_id = message.get("id")

        method = self._pending_requests.pop(msg_id, None)
        if method == "initialize":
            self.init_event.set()
        elif method in ("session/new", "session/load"):
            LOG.error("Session request failed: %s", err_msg)
            self.session_event.set()

        # Try to extract details from data
        data = error.get("data")
        if isinstance(data, dict):
            err_msg = ", ".join([f"{k}:{v}" for k, v in data.items()])

        self.callbacks['on_error'](err_msg + "\n\n")

    def _handle_result(self, msg_id, result):
        """Handle result messages."""
        method = self._pending_requests.pop(msg_id, None)

        if method == "initialize":
            LOG.info("Agent initialize success")
            self.inited = True
            self.agent_capabilities = result.get("agentCapabilities", {})
            agent_info = result.get("agentInfo", {})
            self.agent_version = agent_info.get("version", "0.0.0")
            self.init_event.set()
        elif method in ("session/new", "session/load"):
            if "sessionId" in result:
                self.session_id = result["sessionId"]
            self.session_event.set()
            self.callbacks['on_session_ready']()
        elif method == "session/prompt":
            self.callbacks['on_stop'](msg_id, result.get("stopReason", "end_turn"))
        elif method == "session/cancel":
            LOG.info("Session cancel success")

    def _handle_session_update(self, update):
        """Handle session update messages."""
        if self.ignore_messages and update["sessionUpdate"] in ("agent_message_chunk", "user_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"):
            return

        if update["sessionUpdate"] == "agent_message_chunk":
            text = update["content"].get("text")
            if text:
                self.callbacks['on_message'](text)
        elif update["sessionUpdate"] == "user_message_chunk":
            text = update["content"].get("text")
            if text:
                self.callbacks['on_user_message'](text)
        elif update["sessionUpdate"] == "agent_thought_chunk":
            text = update["content"].get("text")
            if text:
                self.callbacks['on_thought'](text)
        elif update["sessionUpdate"] in ("tool_call", "tool_call_update"):
            self.callbacks['on_tool_call'](update)
        else:
            LOG.debug("unprocessed agent chat content: %s" % update)

    def _handle_permission_request(self, message):
        """Handle permission request from Gemini."""
        self.callbacks['on_permission_request'](
            message["id"],
            message["params"].get("options", []),
            message["params"].get("toolCall", {})
        )

    def _handle_fs_read(self, message):
        """Handle file system read request."""
        params = message.get("params", {})
        msg_id = message.get("id")
        LOG.debug("Received fs/read_text_file request: %s", params.get("path"))
        try:
            file_path = params.get("path")
            if not file_path:
                raise ValueError("Missing 'path' parameter")
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self._send_response(msg_id, {"content": content})
            LOG.info("Successfully read file: %s", file_path)
        except Exception as e:
            LOG.error("Error reading file: %s", e)
            self._send_error_response(msg_id, ErrorCode.InternalError, str(e))

    def _handle_fs_write(self, message):
        """Handle file system write request."""
        params = message.get("params", {})
        msg_id = message.get("id")
        LOG.debug("Received fs/write_text_file request: %s", params.get("path"))
        try:
            file_path = params.get("path")
            content = params.get("content", "")
            if not file_path:
                raise ValueError("Missing 'path' parameter")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self._send_response(msg_id, {"success": True})
            LOG.info("Successfully wrote file: %s", file_path)
        except Exception as e:
            LOG.error("Error writing file: %s", e)
            self._send_error_response(msg_id, ErrorCode.InternalError, str(e))

    def _write_loop(self):
        """Process input queue and send to Gemini."""
        self._agent_initialize()

        # Check version and capability for loadSession
        # User requested version >= 0.34.0
        can_load = self.agent_capabilities.get("loadSession", False) and \
                  _version_greater_or_equal(self.agent_version, "0.34.0")

        if self.session_id and can_load:
            LOG.info("reloading session %s", self.session_id)
            self._agent_session_load(self.session_id)
        else:
            # Clear invalid session_id if we can't load it
            self.session_id = ""
            self._agent_session_new()

        while True:
            item = self.input_queue.get()
            if item is None:
                self.process.stdin.close()
                break
            msg_id, user_input = item
            try:
                self._agent_session_prompt(msg_id, user_input)
            except Exception as e:
                self.callbacks['on_error']("Error writing to process: %s" % e)
                break

        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG.error("terminate gemini process %s", self.process.pid)
                self.process.terminate()
            self.process = None

    def _agent_initialize(self):
        """Initialize the agent."""
        self._send_request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}
            }
        })
        self.init_event.wait(timeout=30)

    def _agent_session_new(self):
        """Create a new session."""
        self._send_request("session/new", {
            "cwd": self.cwd,
            "mcpServers": [],
        })
        self.session_event.wait(timeout=10)

    def _agent_session_load(self, session_id):
        """Load an existing session."""
        self._send_request("session/load", {
            "sessionId": session_id,
            "cwd": self.cwd,
            "mcpServers": [],
        })
        self.session_event.wait(timeout=10)

    def _agent_session_prompt(self, msg_id, input_text):
        """Send a prompt to the session."""
        self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": input_text}]
        }, msg_id=msg_id)

    def agent_session_cancel(self):
        return self._send_request("session/cancel",
            {"sessionId": self.session_id})

    def _next_message_id(self):
        """Generate next message ID."""
        self.message_id += 1
        return self.message_id

    def _send_request(self, method, params, msg_id=None):
        """Send a JSON-RPC request."""
        if msg_id is None:
            msg_id = self._next_message_id()
        self._pending_requests[msg_id] = method
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params:
            request["params"] = params
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        return msg_id

    def _send_response(self, msg_id, resp):
        """Send a JSON-RPC response."""
        request = {"jsonrpc": "2.0", "id": int(msg_id)}
        if resp:
            request["result"] = resp
        LOG.debug("Send response:\n%s" % json.dumps(request, ensure_ascii=False, indent=2))
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

    def _send_error_response(self, msg_id, code, error_msg):
        """Send a JSON-RPC error response."""
        error_response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": error_msg}
        }
        self.process.stdin.write(json.dumps(error_response) + "\n")
        self.process.stdin.flush()
