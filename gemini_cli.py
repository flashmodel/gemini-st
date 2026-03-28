import enum
import logging
import os
import re

import sublime
import sublime_plugin

from .agentclient import GeminiClient
from . import plugin
from .plugin import show_diff

# logger by pachage name
LOG = logging.getLogger(__package__)

CHAT_VIEW_NAME = "Gemini Chat"
PROMPT_PREFIX = "\n❯ "
gemini_clients = {}

GEMINI_APPROVE_MODE = "gemini_approve_mode"

class ApproveMode(enum.Enum):
    DEFAULT = "default"
    ALLOW_EDIT = "allow-edit"
    ACCEPT_ALL = "accept-all"

def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    plugin.update_log_level(settings)


def plugin_unloaded():
    """
    Called by Sublime Text when the plugin is unloaded (e.g., during restart,
    package update, or application quit). Cleans up subprocesses to prevent orphans.
    """
    for window_id, session in list(gemini_clients.items()):
        try:
            LOG.info("Terminating Gemini CLI session for window %s on unload", window_id)
            if session.client and session.client.process:
                session.client.process.terminate()
        except Exception as e:
            LOG.error("Failed to terminate gemini on plugin unload: %s", e)

    gemini_clients.clear()


def _reconnect_chat_view(view):
    """
    Reconnect an existing chat view to a new ChatSession after a restart.
    """
    window = view.window()
    if not window:
        return

    window.run_command("gemini_cli", {"view_id": view.id()})
    LOG.info("Reconnecting Gemini CLI session for window %s", window.id())


def get_best_dir(view):
    window = view.window()
    if window:
        # Check for explicitly set workspace
        custom_cwd = window.settings().get("gemini_active_workspace")
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd

        folders = window.folders()
        if folders:
            return folders[0]
    return ""


class LoadingAnimation:
    """
    Manages a loading animation phantom with start/stop control.
    """
    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "gemini_loading")
        self.is_loading = False
        self.frame_index = 0
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def start(self, region):
        """Start the loading animation at the specified region."""
        if not self.is_loading:
            self.is_loading = True
            self.frame_index = 0
            self.region_provider = region
            self._update_animation()

    def stop(self):
        """Stop the loading animation and clear the phantom."""
        self.is_loading = False
        # Clear on next tick to avoid thread issues if called from background
        sublime.set_timeout(lambda: self.phantom_set.update([]), 0)

    def _update_animation(self):
        """Update the loading animation frame."""
        if not self.is_loading:
            return

        # Resolve current region
        if callable(self.region_provider):
            region = self.region_provider()
        else:
            region = self.region_provider

        frame = self.frames[self.frame_index % len(self.frames)]

        html = f"""
        <body id="gemini-loading">
            <style>
                .loading {{
                    color: var(--accent);
                    font-weight: bold;
                    margin-right: 8px;
                    font-family: var(--font-mono);
                }}
            </style>
            <div class="loading">{frame}</div>
        </body>
        """

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK
        )])

        # Schedule next frame
        self.frame_index += 1
        sublime.set_timeout(lambda: self._update_animation(), 100)


class ChatSession:
    """
    Manages the state and UI for a single Gemini chat session.
    """
    def __init__(self, window, view, initial_msg="", send_immediate=False):
        self.window = window
        self.chat_view = view

        # Permission request state
        self.pending_permissions = {}
        self.shown_tool_calls = set()
        self.phantom_set = sublime.PhantomSet(self.chat_view, "gemini_permissions")
        self.next_phantom_id = 0

        # Permission file edit
        self.pending_diff = {}

        # Loading animation
        self.loading_animation = LoadingAnimation(self.chat_view)

        # Message on chat startup
        self.initial_msg = initial_msg

        # Thought state
        self.thought_blocks = [] # List of {"text": str, "expanded": bool, "pos": int}
        self.current_thought_text = ""
        self.current_thought_id = 0
        self.current_msgid = 0
        self.thought_phantom_set = sublime.PhantomSet(self.chat_view, "gemini_thoughts")
        self.send_immediate = send_immediate
        self.last_is_tool = True

        # Create the Gemini client
        self.client = GeminiClient(
            callbacks={
                'on_message': self.on_message,
                'on_error': self.on_error,
                'on_stop': self.on_stop,
                'on_permission_request': self.on_permission_request,
                'on_session_ready': self.on_session_ready,
                'on_exit': self.on_exit,
                'on_thought': self.on_thought,
                'on_tool_call': self.on_tool_call
            },
            cwd=get_best_dir(self.chat_view)
        )

    def set_initial_msg(self, text):
        """Set or append text to initial_msg."""
        if self.initial_msg:
            self.initial_msg += " " + text
        else:
            self.initial_msg = text

    def loading_region(self):
        """Get the region where the loading animation should be displayed."""
        input_start = self.chat_view.settings().get("gemini_input_start", self.chat_view.size())
        return sublime.Region(input_start, input_start)

    def start(self, api_key, gemini_command=None, extra_env=None):
        self.client.start(api_key, gemini_command, extra_env)
        self.loading_animation.start(self.loading_region)

    def stop(self):
        try:
            if self.client.inited and self.loading_animation.is_loading:
                self.client.agent_session_cancel()
            self.client.stop()
        except Exception:
            pass
        self.loading_animation.stop()

    def send_input(self, user_input):
        prompt_id = self.client.send_input(user_input)
        self.current_msgid = prompt_id

    def on_message(self, text):
        """Handle message chunks from Gemini."""
        # Dispatch to main thread to ensure thread safety for UI updates and state modification
        sublime.set_timeout(lambda: self._on_message_process(text), 0)

    def _on_message_process(self, text):
        # Ensure loading animation is active
        self.loading_animation.start(self.loading_region)

        if self.last_is_tool:
            text = "\n" + text

        self.chat_view.run_command("chat_append", {"text": text})
        self.last_is_tool = False

    def on_error(self, message):
        """Handle error messages."""
        def _on_error_process():
            self.loading_animation.stop()
            self.chat_view.run_command("chat_append", {"text": "\nError: " + message + "\n"})
            self.last_is_tool = False
        sublime.set_timeout(_on_error_process, 0)

    def on_stop(self, msg_id, stop_text):
        """Handle stop signal from Gemini."""
        def _on_stop_process():
            self.loading_animation.stop()
            self.chat_view.run_command("chat_append", {"text": "\n\n"})
            self.last_is_tool = False
        sublime.set_timeout(_on_stop_process, 0)
        LOG.info("prompt %s completed: %s", msg_id, stop_text)

    def on_session_ready(self):
        """Handle session ready notification."""
        self.loading_animation.stop()
        shortcut = "Command+Enter" if sublime.platform() == "osx" else "Control+Enter"
        welcome_text = "Interactive Gemini CLI (ACP Mode)\nType your message and press %s to send.\n\n" % shortcut
        self.chat_view.run_command("append", {"characters": welcome_text})
        self.chat_view.settings().set("gemini_input_start", self.chat_view.size())

        if self.initial_msg:
            self.chat_view.run_command("chat_prompt", {"text": self.initial_msg})
            if self.send_immediate:
                self.send_immediate = False
                self.chat_view.run_command("gemini_send_input")
        else:
            self.chat_view.run_command("chat_prompt", {"text": ""})

    def on_permission_request(self, msg_id, options, tool_call):
        """Handle permission request from Gemini."""
        tool_name = tool_call.get("title", "")
        if not tool_name:
            tool_name = tool_call.get("function", "")

        approve_mode = self.window.settings().get(GEMINI_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
        tool_kind = tool_call.get("kind", "")

        always_confirm_kinds = ("communicate", "plan", "ask_user")
        if tool_kind not in always_confirm_kinds:
            if approve_mode == ApproveMode.ACCEPT_ALL.value:
                if self._auto_approve(msg_id, options, tool_call):
                    return
            elif approve_mode == ApproveMode.ALLOW_EDIT.value:
                risky_kinds = ("execute", "agent")
                if tool_kind not in risky_kinds:
                    if self._auto_approve(msg_id, options, tool_call):
                        return

        phantom_id = self.next_phantom_id
        self.next_phantom_id += 1
        self.pending_permissions[phantom_id] = {"msg_id": msg_id}

        sublime.set_timeout(
            lambda: self.show_permission_phantom(phantom_id, options, tool_call),
            0
        )

    def _output_tool_call_text(self, tool_call):
        """Format and append tool call text to the chat view."""
        # Ensure loading animation is active
        self.loading_animation.start(self.loading_region)

        tool_id = tool_call.get("toolCallId")
        if tool_id and tool_id in self.shown_tool_calls:
            return
        if tool_id:
            self.shown_tool_calls.add(tool_id)

        tool_kind = tool_call.get("kind", "tool")
        tool_title = tool_call.get("title", tool_call.get("name", ""))

        if tool_kind == "execute" and tool_title:
            # Remove content within [ ] from the execution title
            tool_title = re.sub(r'\s*\[.*?\]', '', tool_title)

        formatted_title = f"⏺ {tool_kind.capitalize()}"
        if tool_title:
            formatted_title = f"⏺ {tool_kind.capitalize()} {tool_title}"

        # Determine prefix based on previous output type
        view = self.chat_view
        prefix = ""
        insert_pos = view.settings().get("gemini_input_start", 0)

        if insert_pos > 0:
            # Read up to 2 characters before the insertion point
            start_check = max(0, insert_pos - 2)
            last_chars = view.substr(sublime.Region(start_check, insert_pos))
            last_char = last_chars[-1] if last_chars else ""

            if last_char != "\n":
                prefix = "\n"

            if not self.last_is_tool:
                if last_chars != "\n\n":
                    # Ensure a blank line if previous wasn't a tool and no blank line exists
                    prefix += "\n"

        selected_text = f"{prefix}{formatted_title}\n"
        view.run_command("chat_append", {"text": selected_text})
        self.last_is_tool = True

    def _auto_approve(self, msg_id, options, tool_call):
        """Attempt to auto-approve the permission."""
        allow_option = None

        # Prefer allow_once
        for option in options:
            if option.get("kind", "").lower() == "allow_once":
                allow_option = option
                break
        if not allow_option:
            for option in options:
                option_kind = option.get("kind", "").lower()
                if option_kind in ("allow_always", "allow"):
                    allow_option = option
                    break
        if not allow_option:
            for option in options:
                option_id = option.get("optionId", "").lower()
                if option_id in ("proceed_once", "proceed_always"):
                    allow_option = option
                    break

        if not allow_option:
            option_id = "proceed_once"
        else:
            option_id = allow_option.get("optionId", "proceed_once")

        tool_title = tool_call.get("title", tool_call.get("name", "Unknown Tool"))
        LOG.info(f"Auto-approving {tool_title} with option {option_id}")
        self.client.send_permission_response(msg_id, option_id)

        self._output_tool_call_text(tool_call)
        return True

    def on_thought(self, text):
        """Handle thought chunk from Gemini."""
        sublime.set_timeout(lambda: self._on_thought_process(text), 0)

    def on_tool_call(self, tool_call):
        """Handle tool call update from Gemini."""
        if tool_call.get("status") == "in_progress":
            sublime.set_timeout(lambda: self._output_tool_call_text(tool_call), 0)

    def _on_thought_process(self, text):
        # Ensure loading animation is active
        self.loading_animation.start(self.loading_region)
        self.update_think_process(text)

    def update_think_process(self, text):
        """
        Refresh current thinking text and all thinking phantom
        """
        if self.current_msgid != self.current_thought_id:
            # Start a new thought block
            self.current_thought_id = self.current_msgid
            self.current_thought_text = text
            pos = self.chat_view.settings().get("gemini_input_start", self.chat_view.size())
            self.thought_blocks.append({
                "text": text,
                "expanded": False,
                "pos": pos
            })
            # New line char for the Phantom layout
            self.chat_view.run_command("chat_append", {"text": "\n"})
        else:
            # Append to latest thought block
            self.current_thought_text += text
            if self.thought_blocks:
                self.thought_blocks[-1]["text"] = self.current_thought_text

        self.update_thought_phantom()

    def update_thought_phantom(self):
        """Render all thought phantoms based on current state."""
        phantoms = []
        for i, block in enumerate(self.thought_blocks):
            content = block["text"]
            if not content:
                continue

            # Prepare content for display
            if block["expanded"]:
                # Expanded state
                icon = "▼"
                # Basic HTML escaping
                display_content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                body_style = "display: block;"
            else:
                # Collapsed state
                icon = "▶"
                display_content = ""
                body_style = "display: none;"

            html = f"""
            <body id="gemini-thoughts-{i}">
                <style>
                    .thought-container {{
                        background-color: color(var(--background) blend(var(--foreground) 95%));
                        border-radius: 4px;
                        padding: 0.5rem;
                        margin: 0.5rem 0;
                    }}
                    .thought-header {{
                        font-weight: bold;
                        cursor: pointer;
                        color: var(--accent);
                        text-decoration: none;
                    }}
                </style>
                <div class="thought-container">
                    <a href="toggle_thought_{i}" class="thought-header">{icon} 💡Thinking Process</a>
                    <div style="{body_style} margin-top: 0.5rem; font-family: var(--font-mono); font-size: 0.9em;">
                        {display_content}
                    </div>
                </div>
            </body>
            """

            region = sublime.Region(block["pos"], block["pos"])
            phantoms.append(sublime.Phantom(
                region,
                html,
                sublime.LAYOUT_BLOCK,
                on_navigate=self.handle_thought_navigate
            ))

        self.thought_phantom_set.update(phantoms)

    def handle_thought_navigate(self, href):
        """Handle navigation events from thought phantoms."""
        if href.startswith("toggle_thought_"):
            try:
                index = int(href.replace("toggle_thought_", ""))
                if 0 <= index < len(self.thought_blocks):
                    self.thought_blocks[index]["expanded"] = not self.thought_blocks[index]["expanded"]
                    self.update_thought_phantom()
            except ValueError:
                pass

    def on_exit(self):
        """Handle client exit."""
        sublime.set_timeout(lambda: sublime.status_message("Gemini CLI session ended"), 0)

    def show_permission_phantom(self, phantom_id, options, tool_call):
        """Display a phantom with permission options."""
        html = self.create_permission_phantom_html(phantom_id, options, tool_call)
        input_start = self.chat_view.settings().get("gemini_input_start", self.chat_view.size())
        region = sublime.Region(input_start, input_start)
        phantom = sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=lambda href: self.handle_permission_selection(href, tool_call)
        )
        self.phantom_set.update([phantom])

    def create_permission_phantom_html(self, phantom_id, options, tool_call):
        """Generate HTML for permission request phantom."""
        tool_name = tool_call.get("title", "Unknown tool")
        edit_file = ""
        if tool_call.get("kind") == "edit":
            filetool = tool_call["content"][0]
            if filetool["type"] == "diff":
                tool_id = tool_call["toolCallId"]
                self.pending_diff[tool_id] = filetool
                edit_file = filetool["path"]

        edit_file_html = ""
        if edit_file:
            # Use basename for the label text, tool_id for href to retrieve diff data
            file_name = os.path.basename(edit_file)
            tool_id = tool_call.get("toolCallId", "")
            edit_file_html = f'''
                <a href="open_diff:{tool_id}" style="
                    color: var(--accent);
                    text-decoration: none;
                    background: color(var(--background) blend(var(--foreground) 90%));
                    padding: 2px 4px;
                    border-radius: 3px;
                    font-size: 11px;
                    margin-left: 8px;
                ">{file_name}</a>
            '''

        buttons_html = ""
        for option in options:
            option_id = option.get("optionId", "")
            label = option.get("name", option_id)
            href = "phantom_%d:%s" % (phantom_id, option_id)
            buttons_html += '''
                <a href="%s" style="
                    display: inline-block;
                    padding: 6px 12px;
                    margin: 4px;
                    margin-right: 8px;
                    background: #007acc;
                    color: var(--foreground);
                    font-weight: bold;
                    text-decoration: none;
                    border: 1px solid #007acc;
                    border-radius: 3px;
                    font-size: 12px;
                ">%s</a>
            ''' % (href, label)

        return f'''
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
                ">🔐{edit_file_html} Permission Required: <strong>{tool_name}</strong></div>
                <div>{buttons_html}</div>
            </div>
        '''

    def handle_permission_selection(self, href, tool_call):
        """Handle user clicking on a permission option."""
        try:
            if href.startswith("open_diff:"):
                tool_id = href[len("open_diff:"):]
                if tool_id in self.pending_diff:
                    filetool = self.pending_diff[tool_id]
                    show_diff(
                        self.window,
                        filetool.get("oldText", ""),
                        filetool.get("newText", ""),
                        f"Diff: {os.path.basename(filetool['path'])}"
                    )
                return

            parts = href.split(":", 1)
            if len(parts) != 2 or not parts[0].startswith("phantom_"):
                return

            phantom_id = int(parts[0].replace("phantom_", ""))
            option_id = parts[1]

            if phantom_id not in self.pending_permissions:
                LOG.warning("Permission request %d not found", phantom_id)
                return

            perm_data = self.pending_permissions[phantom_id]
            self.client.send_permission_response(perm_data["msg_id"], option_id)
            self.phantom_set.update([])
            del self.pending_permissions[phantom_id]

            self._output_tool_call_text(tool_call)

        except Exception as e:
            LOG.error("Error handling permission selection: %s", e)


class GeminiCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the Gemini CLI with ACP protocol.
    """
    def run(self, initial_msg="", send_immediate=False, view_id=None):
        # Check if a client already exists for this window
        window_id = self.window.id()
        if window_id in gemini_clients:
            # Try to find and focus existing chat view
            for view in self.window.views():
                if view.settings().get("gemini_chat_view", False):
                    self.window.focus_view(view)
                    sublime.status_message("Gemini: Already active in this window.")
                    return
            # If client exists but no view found, clean up
            del gemini_clients[window_id]

        chat_view = None
        if view_id is not None:
            chat_view = sublime.View(view_id)
            if not chat_view.is_valid() or not chat_view.settings().get("gemini_chat_view", False):
                chat_view = None

        if chat_view:
            chat_view.run_command("append", {"characters": "\n\nReconnecting to Gemini CLI session...\n\n"})
        else:
            # Create a new view to display the result
            chat_view = self.window.new_file()
            chat_view.set_name(CHAT_VIEW_NAME)
            chat_view.set_scratch(True)
            chat_view.set_syntax_file("Packages/GeminiCLI/GeminiChat.sublime-syntax")
            chat_view.settings().set("draw_minimap", False)
            chat_view.settings().set("line_numbers", False)
            chat_view.settings().set("word_wrap", True)
            chat_view.settings().set("gemini_chat_view", True)
            chat_view.run_command("append", {"characters": "Starting Gemini CLI session...\n"})

        cwd = get_best_dir(chat_view)
        if cwd:
            chat_view.run_command("append", {"characters": "cwd: %s\n" % cwd})

        chat_view.settings().set("gemini_input_start", chat_view.size())

        # Create and start the ChatSession
        session = ChatSession(self.window, chat_view, initial_msg=initial_msg, send_immediate=send_immediate)
        gemini_clients[window_id] = session

        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        extra_env = settings.get("env", {})
        session.start(
            settings.get("api_key", "").strip(),
            settings.get("gemini_command", "gemini"),
            extra_env
        )


class GeminiSendInputCommand(sublime_plugin.TextCommand):
    """
    Handles the input submission (bound to Ctrl+Enter).
    """
    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        window_id = window.id()
        if window_id not in gemini_clients:
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

        # Send to client
        gemini_clients[window_id].send_input(user_input)
        LOG.info("User enter prompt %s", user_input)


class GeminiChatViewListener(sublime_plugin.EventListener):
    def on_activated_async(self, view):
        """
        Reconnect an orphaned chat view only when its window gains focus.
        """
        window = view.window()
        if not window:
            return

        window_id = window.id()
        if window_id in gemini_clients:
            return

        # Check if this window contains an orphaned chat view
        for v in window.views():
            if v.settings().get("gemini_chat_view", False):
                _reconnect_chat_view(v)
                break

    def on_close(self, view):
        """
        Cleanup session when the chat view is closed.
        """
        if view.name() == CHAT_VIEW_NAME:
            window = view.window()
            if window is None:
                window = sublime.active_window()

            if window is not None:
                window_id = window.id()
                if window_id in gemini_clients:
                    try:
                        gemini_clients[window_id].stop()
                    except Exception:
                        pass
                    del gemini_clients[window_id]
                    LOG.info("Cleaned up Gemini CLI for window %s" % window_id)

    def on_selection_modified(self, view):
        """
        Restrict cursor movement to the editable area.
        Allows selecting history for copy, but prevents placing the caret in history.
        """
        if not view.settings().get("gemini_chat_view", False) and view.name() != CHAT_VIEW_NAME:
            return
        if not view.settings().has("gemini_input_start"):
            return

        input_start = view.settings().get("gemini_input_start", 0)
        editable_start = input_start + len(PROMPT_PREFIX)

        new_sel = []
        changed = False

        for sel in view.sel():
            # Only restrict empty regions (cursor carets), allowing user to select history to copy
            if sel.empty() and sel.begin() < editable_start:
                new_sel.append(sublime.Region(editable_start))
                changed = True
            else:
                new_sel.append(sel)

        if changed:
            view.sel().clear()
            view.sel().add_all(new_sel)


    def _redirect_cursor(self, view):
        """Helper to move cursor to the end of the view."""
        end_pos = view.size()
        view.sel().clear()
        view.sel().add(sublime.Region(end_pos))
        view.show(end_pos)

    def on_text_command(self, view, command_name, args):
        """Intercept text commands to protect content before prompt area."""
        # Only monitor Gemini chat views
        if not view.settings().get("gemini_chat_view", False) and view.name() != CHAT_VIEW_NAME:
            return None

        input_start = view.settings().get("gemini_input_start", 0)
        editable_start = input_start + len(PROMPT_PREFIX)

        # Handle deletion commands - block if they affect content before prompt
        delete_commands = ("left_delete", "right_delete", "delete_word", "delete_word_backward",
                          "delete_to_mark", "run_macro_file", "cut",)

        if command_name in delete_commands:
            for sel in view.sel():
                # Block deletion if cursor is in protected area
                if sel.begin() < editable_start:
                    self._redirect_cursor(view)
                    return ("noop", {})

                # Special case for backspace: if at the exact boundary,
                # it deletes backward into protected area
                if (command_name in ("left_delete", "delete_word_backward") and
                    sel.empty() and sel.begin() == editable_start):
                    self._redirect_cursor(view)
                    return ("noop", {})

        # Handle insert/modification commands - redirect to end if in protected area
        mod_commands = ("insert", "paste", "insert_characters", "insert_snippet",
                       "append", "yank", "paste_and_indent", "clipboard_history_paste")

        if command_name in mod_commands:
            should_redirect = False
            for sel in view.sel():
                if sel.begin() < editable_start:
                    should_redirect = True
                    break

            if should_redirect:
                self._redirect_cursor(view)
                return ("noop", {})

        return None

    def on_query_completions(self, view, prefix, locations):
        """
        Provide filename completions when typing '@' in the prompt area.
        Shows three categories: open files, current directory files, and subdirectories.
        """
        if not view.settings().get("gemini_chat_view", False):
            return None

        # Check if in editable area
        input_start = view.settings().get("gemini_input_start", 0)
        editable_start = input_start + len(PROMPT_PREFIX)
        pos = locations[0]

        if pos < editable_start:
            return None

        # Check if the prefix is preceded by '@'
        trigger_pos = pos - len(prefix) - 1
        if trigger_pos < 0 or view.substr(trigger_pos) != '@':
            return None

        completions = []
        window = view.window()
        if not window:
            return None

        # Get current directory (first workspace folder)
        current_dir = None
        folders = window.folders()
        if folders:
            current_dir = folders[0]

        # Category 1: Currently open files
        seen_files = set()
        for v in window.views():
            file_path = v.file_name()
            if not file_path:
                continue

            # Skip the chat view itself
            if v.settings().get("gemini_chat_view", False):
                continue

            file_name = os.path.basename(file_path)
            if file_name in seen_files:
                continue

            seen_files.add(file_name)

            # Use relative path as hint if available
            rel_path = file_name
            if current_dir and file_path.startswith(current_dir):
                rel_path = os.path.relpath(file_path, current_dir)

            completions.append(sublime.CompletionItem(
                file_name,
                annotation=f"📂 {rel_path}",
                completion=file_name,
                kind=sublime.KIND_VARIABLE
            ))

        # Category 2: Files in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isfile(item_path) and not item.startswith('.'):
                        if item not in seen_files:
                            seen_files.add(item)
                            completions.append(sublime.CompletionItem(
                                item,
                                annotation="📄 current dir",
                                completion=item,
                                kind=sublime.KIND_AMBIGUOUS
                            ))
            except OSError:
                pass

        # Category 3: Subdirectories in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isdir(item_path) and not item.startswith('.'):
                        completions.append(sublime.CompletionItem(
                            item + "/",
                            annotation="📁 subdirectory",
                            completion=item + "/",
                            kind=sublime.KIND_NAMESPACE
                        ))
            except OSError:
                pass

        return sublime.CompletionList(completions, flags=sublime.INHIBIT_WORD_COMPLETIONS)

    def on_modified_async(self, view):
        """
        Trigger autocompletion immediately when '@' is typed.
        """
        if not view.settings().get("gemini_chat_view", False):
            return

        # Check if the last character typed was '@'
        sel = view.sel()
        if not sel:
            return

        pos = sel[0].begin()
        if pos <= 0:
            return

        # Check if in editable area
        input_start = view.settings().get("gemini_input_start", 0)
        editable_start = input_start + len(PROMPT_PREFIX)
        if pos < editable_start:
            return

        last_char = view.substr(pos - 1)
        if last_char == '@':
            # Run auto_complete command
            view.run_command("auto_complete", {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False
            })


class ChatAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        input_start = self.view.settings().get("gemini_input_start", 0)
        inserted = self.view.insert(edit, input_start, text)
        new_pos = input_start + inserted
        self.view.settings().set("gemini_input_start", new_pos)
        self.view.show(self.view.size())


class ChatPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), "\n\n")
        self.view.settings().set("gemini_input_start", self.view.size())

        # Next input prompt
        self.view.insert(edit, self.view.size(), PROMPT_PREFIX)
        if text:
            self.view.insert(edit, self.view.size(), text + " ")
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)


class GeminiAddContextCommand(sublime_plugin.TextCommand):
    """
    Command to add current file context to the Gemini chat prompt.
    """
    def run(self, edit):
        view = self.view
        window = view.window()
        if not window:
            return

        file_path = view.file_name()
        if not file_path:
            return

        file_name = os.path.basename(file_path)

        # Get line numbers (1-based)
        sel = view.sel()[0]
        row_start, _ = view.rowcol(sel.begin())
        row_end, _ = view.rowcol(sel.end())

        # Format as @file_name#L(A)-(B)
        # Handle single line selection vs range
        if row_start == row_end:
            context_tag = f"@{file_name}#L{row_start + 1}"
        else:
            context_tag = f"@{file_name}#L{row_start + 1}-{row_end + 1}"

        # Find or create Gemini chat view
        chat_view = None
        for v in window.views():
            if v.settings().get("gemini_chat_view", False):
                chat_view = v
                break

        if not chat_view:
            # If no chat view, create one and pass the context tag immediately
            window.run_command("gemini_cli", {"initial_msg": context_tag})
        else:
            window.focus_view(chat_view)
            self._insert_tag(chat_view, context_tag)

    def _insert_tag(self, chat_view, context_tag):
        # Insert at the end of the view (current prompt area)
        end_pos = chat_view.size()
        chat_view.run_command("insert", {"characters": context_tag + " "})
        # Move cursor to end
        chat_view.sel().clear()
        chat_view.sel().add(sublime.Region(chat_view.size()))
        chat_view.show(chat_view.size())


class GeminiAddFileCommand(sublime_plugin.WindowCommand):
    """
    Command to add file reference to the Gemini chat prompt.
    Works from tab context menu and sidebar.
    """
    def run(self, files=None):
        window = self.window
        if not window:
            return

        # Get file path from either files parameter (sidebar) or active view (tab)
        file_path = None
        if files and len(files) > 0:
            file_path = files[0]
        else:
            view = window.active_view()
            if view:
                file_path = view.file_name()

        if not file_path:
            return

        file_name = os.path.basename(file_path)
        context_tag = f"@{file_name}"

        # Find or create Gemini chat view
        chat_view = None
        for v in window.views():
            if v.settings().get("gemini_chat_view", False):
                chat_view = v
                break

        if not chat_view:
            # If no chat view, create one and pass the context tag immediately
            window.run_command("gemini_cli", {"initial_msg": context_tag})
        else:
            window.focus_view(chat_view)
            self._insert_tag(chat_view, context_tag)

    def _insert_tag(self, chat_view, context_tag):
        # Insert at the end of the view (current prompt area)
        chat_view.run_command("insert", {"characters": context_tag + " "})
        # Move cursor to end
        chat_view.sel().clear()
        chat_view.sel().add(sublime.Region(chat_view.size()))
        chat_view.show(chat_view.size())


class GeminiAddFileTextCommand(sublime_plugin.TextCommand):
    """
    Command to add file reference to the Gemini chat prompt from tab context menu.
    """
    def run(self, edit):
        view = self.view
        window = view.window()
        if not window:
            return

        file_path = view.file_name()
        if not file_path:
            return

        file_name = os.path.basename(file_path)
        context_tag = f"@{file_name}"

        # Find or create Gemini chat view
        chat_view = None
        for v in window.views():
            if v.settings().get("gemini_chat_view", False):
                chat_view = v
                break

        if not chat_view:
            # If no chat view, create one and pass the context tag immediately
            window.run_command("gemini_cli", {"initial_msg": context_tag})
        else:
            window.focus_view(chat_view)
            self._insert_tag(chat_view, context_tag)

    def _insert_tag(self, chat_view, context_tag):
        # Insert at the end of the view (current prompt area)
        chat_view.run_command("insert", {"characters": context_tag + " "})
        # Move cursor to end
        chat_view.sel().clear()
        chat_view.sel().add(sublime.Region(chat_view.size()))
        chat_view.show(chat_view.size())

    def is_visible(self):
        # Hide if current view is the Gemini chat view
        return not self.view.settings().get("gemini_chat_view", False)


class GeminiPromptHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "gemini_prompt"

    def placeholder(self):
        return "Enter your prompt for Gemini..."

    def description(self, text):
        return "Gemini: " + text if text else "Gemini Prompt"


class GeminiPromptCommand(sublime_plugin.WindowCommand):
    def run(self, gemini_prompt):
        if not gemini_prompt:
            return

        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            chat_view = session.chat_view
            # self.window.focus_view(chat_view)
            # Use chat_prompt to insert and then send
            chat_view.run_command("insert", {"characters": gemini_prompt})
            chat_view.run_command("gemini_send_input")
        else:
            # Start a new session and send immediately
            self.window.run_command("gemini_cli", {
                "initial_msg": gemini_prompt,
                "send_immediate": True
            })

    def input(self, args):
        return GeminiPromptHandler()


class GeminiSetWorkspaceCommand(sublime_plugin.WindowCommand):
    """
    Sets the active workspace for Gemini based on the selected folder in sidebar.
    """
    def run(self, files=[], dirs=[]):
        # Handle both files and dirs arguments, though typically called with dirs from sidebar
        paths = files + dirs
        LOG.info("set workspace path %s", paths)
        if not paths:
            return

        # Find the first valid directory
        target_dir = None
        for path in paths:
            if os.path.isdir(path):
                target_dir = path
                break
            else:
                # If it's a file, use its parent directory
                parent = os.path.dirname(path)
                if os.path.isdir(parent):
                    target_dir = parent
                    break

        if target_dir:
            self.window.settings().set("gemini_active_workspace", target_dir)
            sublime.status_message(f"Gemini Dir set to: {target_dir}")
        else:
            sublime.status_message("No valid directory for Gemini Workspace")

    def is_visible(self, files=[], dirs=[]):
        # Show only if at least one item is selected
        return bool(files or dirs)

class GeminiApproveModeInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_mode=None):
        self.current_mode = current_mode

    def name(self):
        return "mode"

    def list_items(self):
        items = [
            ("default: ask for confirmation on tool call", ApproveMode.DEFAULT.value),
            ("allow-edit: auto-approve file edits", ApproveMode.ALLOW_EDIT.value),
            ("accept-all: accept all without asking", ApproveMode.ACCEPT_ALL.value),
        ]

        if self.current_mode:
            for i, item in enumerate(items):
                if item[1] == self.current_mode:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        if self.current_mode:
            return f" ( {self.current_mode} ); select approve mode on tool call"
        return "select approve mode on tool call"


class GeminiSetApproveModeCommand(sublime_plugin.WindowCommand):
    """Set permission approve mode for the current Gemini session."""
    def run(self, mode):
        self.window.settings().set(GEMINI_APPROVE_MODE, mode)
        sublime.status_message(f"Approve mode set to: {mode}")

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(GEMINI_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
            return GeminiApproveModeInputHandler(current_mode)
        return None

