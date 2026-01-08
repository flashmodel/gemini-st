import subprocess
import threading
import queue
import json
import logging
import os

LOG = logging.getLogger(__package__)

class ErrorCode:
    AuthRequired = -32000
    InternalError = -32603


class GeminiClient:
    """
    Handles Gemini CLI protocol communication and message processing.
    """
    def __init__(self, callbacks, cwd=None):
        self.callbacks = callbacks
        self.cwd = cwd if cwd else os.path.expanduser("~")
        self.process = None
        self.input_queue = queue.Queue()
        self.message_id = 0
        self.session_id = ""
        self.inited = False
        self.init_event = threading.Event()
        self.session_event = threading.Event()

    def start(self, api_key=None, gemini_command=None):
        """Start the Gemini CLI process and communication threads."""
        if not gemini_command:
            gemini_command = "gemini"
        try:
            env = None
            if api_key:
                env = os.environ.copy()
                env["GOOGLE_API_KEY"] = api_key
                LOG.info("Starting Gemini CLI with custom API key from settings")

            LOG.info("Gemini CLI start cwd=%s", self.cwd)
            self.process = subprocess.Popen(
                [gemini_command, "--experimental-acp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=env,
                encoding='utf-8',
                universal_newlines=True,
                bufsize=1
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
        msgid = self._next_message_id()
        self.input_queue.put(text)
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

        # Try to extract details from data
        data = error.get("data")
        if isinstance(data, dict):
            err_msg = ", ".join([f"{k}:{v}" for k, v in data.items()])

        self.callbacks['on_error'](err_msg + "\n\n")

    def _handle_result(self, msg_id, result):
        """Handle result messages."""
        if "agentCapabilities" in result:
            LOG.info("Agent initialize success")
            self.inited = True
            self.init_event.set()
        elif "sessionId" in result:
            self.session_id = result["sessionId"]
            self.session_event.set()
            self.callbacks['on_session_ready']()
        elif "stopReason" in result:
            self.callbacks['on_stop'](msg_id, result["stopReason"])

    def _handle_session_update(self, update):
        """Handle session update messages."""
        if update["sessionUpdate"] == "agent_message_chunk":
            text = update["content"].get("text")
            if text:
                self.callbacks['on_message'](text)
        elif update["sessionUpdate"] == "agent_thought_chunk":
            text = update["content"].get("text")
            if text:
                self.callbacks['on_thought'](text)
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
        self._agent_session_new()

        while True:
            user_input = self.input_queue.get()
            if user_input is None:
                self.process.stdin.close()
                break
            try:
                self._agent_session_prompt(user_input)
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

    def _agent_session_prompt(self, input_text):
        """Send a prompt to the session."""
        self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": input_text}]
        })

    def agent_session_cancel(self):
        msg_id = self._next_message_id()
        self._send_request("session/cancel",
            {"sessionId": self.session_id})
        return msg_id

    def _next_message_id(self):
        """Generate next message ID."""
        self.message_id += 1
        return self.message_id

    def _send_request(self, method, params):
        """Send a JSON-RPC request."""
        msg_id = self.message_id
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
