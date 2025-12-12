import sublime
import sublime_plugin
import subprocess
import threading
import queue

CHAT_VIEW_NAME = "Gemini Chat"
PROMPT_PREFIX = "❯ "


input_queues = {}


class GeminiCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the Gemini CLI.
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

        welcome_text = "Interactive Gemini CLI\nType your message and press Command+Enter to send.\n\n"
        self.chat_view.run_command("append", {"characters": welcome_text})
        self.chat_view.settings().set("gemini_input_start", self.chat_view.size())

        # Initialize first prompt
        self.chat_view.run_command("chat_prompt", {"text": ""})

        sublime.status_message("Calling Gemini CLI, please wait...")


class GeminiSendInputCommand(sublime_plugin.TextCommand):
    """
    Handles the input submission (bound to Ctrl+Enter).
    """
    def run(self, edit):
        input_start = self.view.settings().get("gemini_input_start", 0)
        input_region = sublime.Region(input_start + len(PROMPT_PREFIX), self.view.size())
        user_input = self.view.substr(input_region).strip()
        sublime.status_message("Chat prompt send")

        # Show input text and next prompt
        self.view.run_command("chat_prompt", {"text": ""})
        self.run_async(user_input)

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
                stderr=subprocess.STDOUT,
                shell=False,
                universal_newlines=True,
                bufsize=1,
            )

            threading.Thread(target=self.stream_output, args=(process,), daemon=True).start()

        except FileNotFoundError:
            print("gemini-cli command not found")

        except Exception as e:
            print("gemini-cli exec error %s" % e)


    def stream_output(self, process):
        """Read stdout line by line and append to the result view."""

        for line in iter(process.stdout.readline, ""):
            if line:
                # render in the chat tab with view append
                sublime.set_timeout(
                    lambda l=line: self.view.run_command(
                        "chat_append",
                        {"text": l}
                    ),
                    0,
                )

        process.wait()
        # Finalize view with status
        sublime.status_message("Gemini CLI chat completed")


class GeminiChatViewListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if view.name() == CHAT_VIEW_NAME:
            window = view.window()
            if window is None:
                window = sublime.active_window()

            if window is not None:
                window_id = window.id()
                if window_id in input_queues:
                    # try:
                    #     # Use the None to quit the input_queue
                    #     input_queues[window_id].put(None)
                    # except Exception:
                    #     pass
                    del input_queues[window_id]
                    print("Cleaned up Gemini CLI for window %s" % window_id)


class ChatAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        input_start = self.view.settings().get("gemini_input_start", 0)
        inserted = self.view.insert(edit, input_start, text + "\n")
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
