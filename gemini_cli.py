import subprocess
import threading
import queue
import time
import json
import logging
import os

import sublime
import sublime_plugin

# logger by pachage name
LOG = logging.getLogger(__package__)

CHAT_VIEW_NAME = "Gemini Chat"
PROMPT_PREFIX = "\n❯ "

input_queues = {}
# plugin settings file
settings = None


def get_log_level(level_name):
    """Maps log level names to logging constants."""
    return getattr(logging, level_name.upper(), logging.INFO)


def update_log_level():
    """
    Reads the log_level from settings and reconfigures the logger.
    """
    level_name = settings.get("log_level", "INFO")
    level = get_log_level(level_name)
    LOG.setLevel(level)
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        LOG.addHandler(handler)

    LOG.info("gemini_cli level set to %s", level)


def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    global settings
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    update_log_level()


def get_best_dir(view):
    folders = view.window().folders()
    if folders:
        return folders[0]

    # file_path = view.file_name()
    # if file_path:
    #     return os.path.dirname(file_path)
    return os.path.expanduser("~")


class GeminiCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the Gemini CLI with ACP protocol.
    """
    def run(self):
        # Create a new view to display the result
        self.chat_view = self.window.new_file()
        self.chat_view.set_name(CHAT_VIEW_NAME)
        self.chat_view.set_scratch(True)
        self.chat_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")
        self.chat_view.settings().set("line_numbers", False)
        self.chat_view.settings().set("word_wrap", True)
        # Context for key bindings
        self.chat_view.settings().set("gemini_chat_view", True)


        self.input_queue = queue.Queue()
        input_queues[self.window.id()] = self.input_queue

        self.chat_view.run_command("append", {"characters": "Starting Gemini CLI session...\n\n"})

        # Start the session in a separate thread
        self.message_id = 0
        self.session_id = ""
        self.inited = False
        self.init_event = threading.Event()
        self.session_event = threading.Event()

        # Permission request state
        self.pending_permissions = {}  # phantom_id -> {msg_id, process, options}
        self.phantom_set = sublime.PhantomSet(self.chat_view, "gemini_permissions")
        self.next_phantom_id = 0
        threading.Thread(target=self.start_session).start()

    def start_session(self):
        gemini_command = "gemini"
        try:
            # Start the Gemini CLI process with --experimental-acp
            # Python 3.3 compatible Popen arguments
            process = subprocess.Popen(
                [gemini_command, "--experimental-acp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                universal_newlines=True, # Text mode
                bufsize=1 # Line buffered
            )

            # Start reader thread
            threading.Thread(target=self.read_loop, args=(process,), daemon=True).start()

            # Start writer loop (runs in this thread)
            self.write_loop(process)

        except FileNotFoundError:
            self.append_error("gemini-cli command not found")
        except Exception as e:
            self.append_error("gemini-cli exec error: %s" % e)

    def read_loop(self, process):
        """Read stdout line by line and append to the result view."""
        try:
            for line in iter(process.stdout.readline, ""):
                if line:
                    LOG.debug("Read line: %s" % line)
                    message = json.loads(line.strip())

                    if "result" in message:
                        resp = message["result"]
                        if "agentCapabilities" in resp:
                            LOG.info("Agent initialize success")
                            self.inited = True
                            self.init_event.set()
                        elif "sessionId" in resp:
                            self.session_id = message["result"]["sessionId"]
                            self.session_event.set()
                        elif "stopReason" in resp:
                            self.chat_view.run_command("chat_append", {"text": "\n\n"})
                    elif "error" in message:
                        self.chat_view.run_command(
                            "chat_append",
                            {"text": message["error"]["message"]+ "\n\n"}
                        )
                    elif message.get("method") == "session/update":
                        if message["params"]["update"]["sessionUpdate"] == "agent_thought_chunk":
                            pass
                        elif message["params"]["update"]["sessionUpdate"] == "agent_message_chunk":
                            self.chat_view.run_command("chat_append",
                                {"text": message["params"]["update"]["content"].get("text")})
                        else:
                            LOG.debug("unprocessed agent chat content: %s" % message)

                    elif message.get("method") == "session/request_permission":
                        LOG.info("Received permission request: %s", message["params"])
                        # Create phantom with options
                        options = message["params"].get("options", [])
                        tool_call = message["params"].get("toolCall", {})

                        # Generate unique phantom ID
                        phantom_id = self.next_phantom_id
                        self.next_phantom_id += 1

                        # Store permission request data
                        self.pending_permissions[phantom_id] = {
                            "msg_id": message["id"],
                            "process": process,
                            "options": options
                        }

                        # Create and add phantom
                        sublime.set_timeout(
                            lambda: self.show_permission_phantom(phantom_id, options, tool_call),
                            0
                        )
                    else:
                        LOG.info("unprocessed message: %s" % message)
            LOG.info("gemini stdio closed")
        except Exception as e:
            LOG.error("gemini read stdout error: %s", e)
        finally:
            LOG.info("gemini cli session ended")
            sublime.status_message("Gemini CLI session ended")

    def write_loop(self, process):
        """Wait for input from queue and write to process stdin."""

        self.agent_initialize(process)
        self.agent_session_new(process)

        # Initialize first prompt
        welcome_text = "Interactive Gemini CLI (ACP Mode)\nType your message and press Command+Enter to send.\n\n"
        self.chat_view.run_command("append", {"characters": welcome_text})
        self.chat_view.settings().set("gemini_input_start", self.chat_view.size())

        self.chat_view.run_command("chat_prompt", {"text": ""})

        while True:
            user_input = self.input_queue.get()
            if user_input is None:
                # exit subprocess
                process.stdin.close()
                break

            try:
                self.agent_session_prompt(process, user_input)
            except Exception as e:
                self.append_error("Error writing to process: %s" % e)
                break

        try:
            code = process.wait(timeout=3)
            LOG.info("exit gemini cli")
        except subprocess.TimeoutExpired:
            process.kill()
            LOG.info("terminate gemini subprocess")

    def agent_initialize(self, process):
        msg_id = self.send_request(process.stdin, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True}
            }
        })

        # Wait asynchronously for initialization to complete
        if self.init_event.wait(timeout=30):
            return msg_id
        else:
            LOG.warning("Agent initialization timeout")
            return None

    def agent_session_new(self, process):
        msg_id = self.send_request(process.stdin, "session/new", {
            "cwd": get_best_dir(self.chat_view),
            "mcpServers": [],
        })

        # Wait asynchronously for session creation to complete
        if self.session_event.wait(timeout=30):
            return msg_id
        else:
            LOG.warning("Session creation timeout")
            return msg_id

    def agent_session_prompt(self, process, input_text):
        msg_id = self.send_request(process.stdin, "session/prompt", {
            "sessionId": self.session_id,
            "prompt": [
                {
                    "type": "text",
                    "text": input_text
                }
            ]
        })

    def agent_session_permission(self, process, input_text):
        msg_id = self.send_request(process.stdin, "session/prompt", {
            "sessionId": self.session_id,
            "prompt": [
                {
                    "type": "text",
                    "text": input_text
                }
            ]
        })

    def append_error(self, message):
        sublime.set_timeout(
            lambda: self.chat_view.run_command("chat_append", {"text": "\nError: " + message + "\n"}),
            0
        )

    def next_message_id(self):
        """
        Calc next message id
        """
        self.message_id += 1
        return self.message_id

    def send_request(self, fd, method, params):
        """
        Send jsonrpc request
        """
        msg_id = self.next_message_id()

        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params:
            request["params"] = params

        LOG.debug("Send request:\n%s" % json.dumps(request, ensure_ascii=False, indent=2))

        request_json = json.dumps(request) + "\n"
        fd.write(request_json)
        fd.flush()
        return msg_id

    def send_response(self, fd, msg_id, resp):
        """
        Send jsonrpc result
        """
        request = {
            "jsonrpc": "2.0",
            "id": int(msg_id),
        }
        if resp:
            request["result"] = resp

        LOG.debug("Send request:\n%s" % json.dumps(request, ensure_ascii=False, indent=2))

        request_json = json.dumps(request) + "\n"
        fd.write(request_json)
        fd.flush()
        return msg_id

    def show_permission_phantom(self, phantom_id, options, tool_call):
        """
        Display a phantom with permission options
        """
        # Get tool call description
        tool_name = tool_call.get("toolName", "Unknown tool")

        # Create HTML for the phantom
        html = self.create_permission_phantom_html(phantom_id, options, tool_name)

        # Get current position (before the input prompt)
        input_start = self.chat_view.settings().get("gemini_input_start", self.chat_view.size())
        region = sublime.Region(input_start, input_start)

        # Create and add phantom
        phantom = sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=lambda href: self.handle_permission_selection(href)
        )

        self.phantom_set.update([phantom])

    def create_permission_phantom_html(self, phantom_id, options, tool_name):
        """
        Generate HTML for permission request phantom
        """
        # Build buttons HTML
        buttons_html = ""
        for option in options:
            option_id = option.get("optionId", "")
            label = option.get("label", option_id)

            # Encode the selection data
            href = "phantom_%d:%s" % (phantom_id, option_id)

            buttons_html += '''
                <a href="%s" style="
                    display: inline-block;
                    padding: 6px 12px;
                    margin: 4px;
                    background: #007acc;
                    color: #ffffff;
                    text-decoration: none;
                    border-radius: 3px;
                    font-size: 12px;
                ">%s</a>
            ''' % (href, label)

        html = '''
            <div style="
                background: #2d2d30;
                padding: 12px;
                margin: 8px 0;
                border-left: 3px solid #007acc;
                border-radius: 3px;
            ">
                <div style="
                    color: #cccccc;
                    font-size: 13px;
                    margin-bottom: 8px;
                ">🔐 Permission Required: <strong>%s</strong></div>
                <div>%s</div>
            </div>
        ''' % (tool_name, buttons_html)

        return html

    def handle_permission_selection(self, href):
        """
        Handle user clicking on a permission option
        """
        try:
            # Parse the href: "phantom_<id>:<option_id>"
            parts = href.split(":", 1)
            if len(parts) != 2 or not parts[0].startswith("phantom_"):
                return

            phantom_id = int(parts[0].replace("phantom_", ""))
            option_id = parts[1]

            # Get the pending permission request
            if phantom_id not in self.pending_permissions:
                LOG.warning("Permission request %d not found", phantom_id)
                return

            perm_data = self.pending_permissions[phantom_id]

            # Send response to gemini CLI
            self.send_response(
                perm_data["process"].stdin,
                perm_data["msg_id"],
                {"outcome": {"outcome": "selected", "optionId": option_id}}
            )

            # Clear the phantom
            self.phantom_set.update([])

            # Clean up
            del self.pending_permissions[phantom_id]

            LOG.info("Permission selected: %s", option_id)

        except Exception as e:
            LOG.error("Error handling permission selection: %s", e)



class GeminiSendInputCommand(sublime_plugin.TextCommand):
    """
    Handles the input submission (bound to Ctrl+Enter).
    """
    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        window_id = window.id()
        if window_id not in input_queues:
            sublime.status_message("No active Gemini session found")
            return

        input_start = self.view.settings().get("gemini_input_start", 0)
        input_region = sublime.Region(input_start + len(PROMPT_PREFIX), self.view.size())
        user_input = self.view.substr(input_region).strip()

        if not user_input:
            return

        sublime.status_message("Sending message...")

        # Show input text and next prompt (simulated local echo/confirmation)
        self.view.run_command("chat_prompt", {"text": ""})

        # Send to queue
        input_queues[window_id].put(user_input)


class GeminiChatViewListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if view.name() == CHAT_VIEW_NAME:
            window = view.window()
            if window is None:
                window = sublime.active_window()

            if window is not None:
                window_id = window.id()
                if window_id in input_queues:
                    try:
                        # Use the None to quit the input_queue
                        input_queues[window_id].put(None)
                    except Exception:
                        pass
                    del input_queues[window_id]
                    LOG.info("Cleaned up Gemini CLI for window %s" % window_id)


class ChatAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        input_start = self.view.settings().get("gemini_input_start", 0)
        inserted = self.view.insert(edit, input_start, text)
        new_pos = input_start + inserted
        self.view.settings().set("gemini_input_start", new_pos)


class ChatPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), "\n\n")
        self.view.settings().set("gemini_input_start", self.view.size())

        # Next input prompt
        self.view.insert(edit, self.view.size(), PROMPT_PREFIX)
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)
