import subprocess
import threading
import queue
import time
import json
import logging
import os

import sublime
import sublime_plugin

from .agentclient import GeminiClient
from . import plugin

# logger by pachage name
LOG = logging.getLogger(__package__)

CHAT_VIEW_NAME = "Gemini Chat"
PROMPT_PREFIX = "\n❯ "
gemini_clients = {}

def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    plugin.update_log_level(settings)


def get_best_dir(view):
    folders = view.window().folders()
    if folders:
        return folders[0]
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
        self.chat_view.settings().set("gemini_chat_view", True)

        self.chat_view.run_command("append", {"characters": "Starting Gemini CLI session...\n\n"})

        # Permission request state
        self.pending_permissions = {}
        self.phantom_set = sublime.PhantomSet(self.chat_view, "gemini_permissions")
        self.next_phantom_id = 0

        # Create and start the Gemini client
        self.client = GeminiClient(
            callbacks={
                'on_message': self.on_message,
                'on_error': self.on_error,
                'on_stop': self.on_stop,
                'on_permission_request': self.on_permission_request,
                'on_session_ready': self.on_session_ready,
                'on_exit': self.on_exit
            },
            cwd=get_best_dir(self.chat_view)
        )
        gemini_clients[self.window.id()] = self.client

        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        self.client.start(settings.get("api_key", "").strip())

    def on_message(self, text):
        """Handle message chunks from Gemini."""
        self.chat_view.run_command("chat_append", {"text": text})

    def on_error(self, message):
        """Handle error messages."""
        sublime.set_timeout(
            lambda: self.chat_view.run_command("chat_append", {"text": "\nError: " + message + "\n"}),
            0
        )

    def on_stop(self):
        """Handle stop signal from Gemini."""
        self.chat_view.run_command("chat_append", {"text": "\n\n"})

    def on_session_ready(self):
        """Handle session ready notification."""
        welcome_text = "Interactive Gemini CLI (ACP Mode)\nType your message and press Command+Enter to send.\n\n"
        self.chat_view.run_command("append", {"characters": welcome_text})
        self.chat_view.settings().set("gemini_input_start", self.chat_view.size())
        self.chat_view.run_command("chat_prompt", {"text": ""})

    def on_permission_request(self, msg_id, options, tool_call):
        """Handle permission request from Gemini."""
        phantom_id = self.next_phantom_id
        self.next_phantom_id += 1
        self.pending_permissions[phantom_id] = {"msg_id": msg_id}

        sublime.set_timeout(
            lambda: self.show_permission_phantom(phantom_id, options, tool_call),
            0
        )

    def on_exit(self):
        """Handle client exit."""
        sublime.status_message("Gemini CLI session ended")

    def show_permission_phantom(self, phantom_id, options, tool_call):
        """Display a phantom with permission options."""
        tool_name = tool_call.get("title", "Unknown tool")
        html = self.create_permission_phantom_html(phantom_id, options, tool_name)
        input_start = self.chat_view.settings().get("gemini_input_start", self.chat_view.size())
        region = sublime.Region(input_start, input_start)
        phantom = sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=lambda href: self.handle_permission_selection(href)
        )
        self.phantom_set.update([phantom])

    def create_permission_phantom_html(self, phantom_id, options, tool_name):
        """Generate HTML for permission request phantom."""
        buttons_html = ""
        for option in options:
            option_id = option.get("optionId", "")
            label = option.get("label", option_id)
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

        return '''
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

    def handle_permission_selection(self, href):
        """Handle user clicking on a permission option."""
        try:
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


class GeminiChatViewListener(sublime_plugin.EventListener):
    def on_close(self, view):
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
