import sublime
import sublime_plugin
import subprocess
import threading
import os

CHAT_VIEW_NAME = "Gemini Chat"
# PROMPT_PREFIX = "\033[1;32m❯ \033[0m"
PROMPT_PREFIX = "❯ "

class GeminiCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the Gemini CLI.
    """
    def run(self):
        # Get the text selected by the user in the current view
        selected_text = ""
        # view = sublime.active_window().active_view()
        # for region in view.sel():
        #     selected_text += view.substr(region)

        selected_text = "/about"

        # Create a new view to display the result
        self.chat_view = self.window.new_file()
        self.chat_view.set_name(CHAT_VIEW_NAME)
        self.chat_view.set_scratch(True)
        self.chat_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")
        self.chat_view.settings().set("line_numbers", False)
        self.chat_view.settings().set("word_wrap", True)

        welcome_text = "Type your message below and press Ctrl+Enter to send.\n\n"
        self.chat_view.run_command("append", {"characters": welcome_text})
        self.chat_view.settings().set("gemini_input_start", self.chat_view.size())

        self.chat_view.run_command("append", {"characters": PROMPT_PREFIX})
        self.chat_view.run_command("append", {"characters": selected_text})
        self.chat_view.show(self.chat_view.size())

        sublime.status_message("Calling Gemini CLI, please wait...")
        self.run_async(selected_text)

    def run_async(self, input_text):
        """
        Execute the Gemini CLI asynchronously in a background thread.
        """
        thread = threading.Thread(target=self.execute_cli, args=(input_text,))
        thread.start()

    def execute_cli(self, input_text):
        """
        The function that actually executes the Gemini CLI command.
        """
        # Make sure your Gemini CLI executable is in the system's PATH
        # If not, provide the full path, e.g., "/usr/local/bin/gemini-cli"
        gemini_command = "gemini"

        try:
            # Start the Gemini CLI process and stream its output line by line
            process = subprocess.Popen(
                [gemini_command, input_text],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                encoding="utf-8",
            )

            threading.Thread(target=self.stream_output, args=(process,), daemon=True).start()

        except FileNotFoundError:
            print("gemini-cli command not found\n")

        except Exception as e:
            print(f"An unknown error occurred while executing the command {e}\n")

    def stream_output(self, process):
        """Read stdout line by line and append to the result view."""
        # Ensure chat_view exists
        if not hasattr(self, "chat_view") or self.chat_view is None:
            return

        sublime.set_timeout(
            lambda: self.chat_view.run_command("chat_send", {"text": ""}),
            0,
        )

        for line in iter(process.stdout.readline, ""):
            if line:
                # render in the chat tab with view append
                sublime.set_timeout(
                    lambda l=line: self.chat_view.run_command(
                        "chat_append",
                        {"text": l}
                    ),
                    0,
                )
        # Capture any remaining stderr after stdout is done
        err = process.stderr.read()
        if err:
            print(f"read stderr {err}\n")

        process.wait()
        # Finalize view with status
        print("exit chat process\n")
        sublime.status_message("Gemini CLI chat completed")


class ChatAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        input_start = self.view.settings().get("gemini_input_start", 0)
        inserted = self.view.insert(edit, input_start, text)
        new_pos = input_start + inserted
        self.view.settings().set("gemini_input_start", new_pos)


class ChatSendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), "\n\n")
        self.view.settings().set("gemini_input_start", self.view.size())

        # Next input prompt
        self.view.insert(edit, self.view.size(), PROMPT_PREFIX)
        self.view.show(self.view.size())
