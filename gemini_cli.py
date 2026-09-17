import enum
import json
import logging
import os
import re

import sublime
import sublime_plugin

from .agents import (
    GeminiClient,
    AntigravityClient,
    list_antigravity_sessions,
    get_antigravity_session_tail,
)
from .chat import (
    AntigravityArtifactManager,
    ArtifactItem,
    AutoComplete,
    open_local_file_link,
    open_tool_file_link,
)
from . import plugin
from .plugin import show_diff

# logger by pachage name
LOG = logging.getLogger(__package__)

CHAT_VIEW_NAME_GEMINI = "Gemini Chat"
CHAT_VIEW_NAME_ANTIGRAVITY = "Antigravity Chat"
CHAT_VIEW_NAMES = {CHAT_VIEW_NAME_GEMINI, CHAT_VIEW_NAME_ANTIGRAVITY}
CHAT_VIEW_NAME = CHAT_VIEW_NAME_GEMINI

PROMPT_PREFIX = "\n❯ "
GEMINI_INPUT_START = "gemini_input_start"
GEMINI_INPUT_ANCHOR = "gemini_input_anchor"
GEMINI_CHAT_VIEW = "gemini_chat_view"
GEMINI_ACTIVE_WORKSPACE = "gemini_active_workspace"
GEMINI_SESSION_ID = "gemini_session_id"
GEMINI_PREFERENCES_CHANGE_KEY = "gemini_cli_packages_preferences"
gemini_clients = {}
preferences_redraw_scheduled = False

GEMINI_APPROVE_MODE = "gemini_approve_mode"
GEMINI_MODEL = "gemini_model"
GEMINI_EFFORT = "gemini_effort"
GEMINI_AGENT = "gemini_agent"
AGENT_GEMINI = "gemini"
AGENT_ANTIGRAVITY = "antigravity"


def get_chat_title(agent_name=None):
    """Return the appropriate chat tab title for the given agent (default: 'Gemini Chat')."""
    if agent_name == AGENT_ANTIGRAVITY:
        return CHAT_VIEW_NAME_ANTIGRAVITY
    return CHAT_VIEW_NAME_GEMINI


def update_chat_title(view, agent=None):
    """Directly determine current agent and update the chat view tab title.
    Defaults to 'Gemini Chat' unless Antigravity is active.
    """
    if not view:
        return
    if hasattr(view, "is_valid") and not view.is_valid():
        return

    window = view.window() if hasattr(view, "window") else None
    current_agent = agent
    if not current_agent and hasattr(view, "settings"):
        current_agent = get_current_agent(window, view)

    if current_agent == AGENT_ANTIGRAVITY:
        title = CHAT_VIEW_NAME_ANTIGRAVITY
    else:
        title = CHAT_VIEW_NAME_GEMINI

    if hasattr(view, "name") and view.name() != title:
        view.set_name(title)
    elif not hasattr(view, "name"):
        view.set_name(title)



def get_current_agent(window=None, view=None):
    """Retrieve the active agent name ('gemini' or 'antigravity')."""
    if view and view.settings().has(GEMINI_AGENT):
        return view.settings().get(GEMINI_AGENT)
    if window and window.settings().has(GEMINI_AGENT):
        return window.settings().get(GEMINI_AGENT)
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    return settings.get("agent", AGENT_ANTIGRAVITY)


def get_antigravity_skip_permissions(window=None, view=None):
    """Retrieve the setting for skipping permissions in Antigravity mode."""
    if view and view.settings().has("antigravity_skip_permissions"):
        return bool(view.settings().get("antigravity_skip_permissions"))
    if window and window.settings().has("antigravity_skip_permissions"):
        return bool(window.settings().get("antigravity_skip_permissions"))
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    return bool(settings.get("antigravity_skip_permissions", True))


def get_current_effort(window=None, view=None, agent=None):
    """Retrieve the active reasoning effort level ('low', 'medium', 'high'), or None."""
    if not agent:
        agent = get_current_agent(window, view)
    agent_effort_key = f"gemini_effort_{agent}"
    if view and view.settings().has(agent_effort_key):
        return view.settings().get(agent_effort_key)
    if window and window.settings().has(agent_effort_key):
        return window.settings().get(agent_effort_key)
    if view and view.settings().has(GEMINI_EFFORT):
        return view.settings().get(GEMINI_EFFORT)
    if window and window.settings().has(GEMINI_EFFORT):
        return window.settings().get(GEMINI_EFFORT)
    settings = sublime.load_settings("GeminiCLI.sublime-settings")
    return settings.get("effort", None)


def get_current_model(window=None, view=None, agent=None):
    """Retrieve the active model for the specified or active agent."""
    if not agent:
        agent = get_current_agent(window, view)

    agent_model_key = f"gemini_model_{agent}"
    if view and view.settings().has(agent_model_key):
        val = view.settings().get(agent_model_key)
        if val:
            return val
    if window and window.settings().has(agent_model_key):
        val = window.settings().get(agent_model_key)
        if val:
            return val

    # For gemini agent, fallback to legacy GEMINI_MODEL setting if present
    if agent == AGENT_GEMINI:
        if view and view.settings().has(GEMINI_MODEL):
            return view.settings().get(GEMINI_MODEL)
        if window and window.settings().has(GEMINI_MODEL):
            return window.settings().get(GEMINI_MODEL)
        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        return settings.get("model", "default")

    # For Antigravity (and other agents), default to None (let agent CLI decide)
    return None


def format_model_tag(model=None, effort=None):
    """Format an in-buffer model notification tag.

    Examples:
        format_model_tag("gemini-3.8-flash", "high") -> "[Model: gemini-3.8-flash:high]"
        format_model_tag("gemini-3.8-flash", None)   -> "[Model: gemini-3.8-flash]"
        format_model_tag(None, "high")               -> "[Model: default:high]"
        format_model_tag(None, None)                 -> "[Model: default]"
    """
    m = (
        model.strip()
        if (model and model.strip().lower() not in ("default", ""))
        else "default"
    )
    eff = (
        effort.strip().lower()
        if (effort and effort.strip().lower() not in ("default", ""))
        else None
    )
    if eff:
        return f"[Model: {m}:{eff}]"
    return f"[Model: {m}]"


def set_input_start(view, pos):
    """Anchor the newline immediately preceding the live input line."""
    view.settings().set(GEMINI_INPUT_START, pos)
    if pos + 1 <= view.size():
        view.add_regions(
            GEMINI_INPUT_ANCHOR,
            [sublime.Region(pos, pos + 1)],
            flags=sublime.HIDDEN | sublime.PERSISTENT
        )
    else:
        view.erase_regions(GEMINI_INPUT_ANCHOR)


def get_input_start(view, default=None):
    """Return the moving input anchor, falling back to persisted settings."""
    if default is None:
        default = view.size()
    regions = view.get_regions(GEMINI_INPUT_ANCHOR)
    if regions and not regions[0].empty():
        pos = regions[0].begin()
        if view.settings().get(GEMINI_INPUT_START) != pos:
            view.settings().set(GEMINI_INPUT_START, pos)
        return pos
    if not view.settings().has(GEMINI_INPUT_START):
        return default
    pos = min(view.settings().get(GEMINI_INPUT_START), view.size())
    set_input_start(view, pos)
    return pos


def input_editable_start(view):
    """The prompt marker is a phantom, so buffer text starts at column zero."""
    return get_input_start(view, 0) + 1


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
    _watch_preferences()


def plugin_unloaded():
    """
    Called by Sublime Text when the plugin is unloaded (e.g., during restart,
    package update, or application quit). Cleans up subprocesses to prevent orphans.
    """
    _unwatch_preferences()

    for window_id, session in list(gemini_clients.items()):
        try:
            LOG.info("Terminating Gemini CLI session for window %s on unload", window_id)
            if session.client and session.client.process:
                session.client.process.terminate()
        except Exception as e:
            LOG.error("Failed to terminate gemini on plugin unload: %s", e)

    gemini_clients.clear()


def _watch_preferences():
    """Watch preference changes that can invalidate minihtml."""
    settings = sublime.load_settings("Preferences.sublime-settings")
    settings.clear_on_change(GEMINI_PREFERENCES_CHANGE_KEY)
    settings.add_on_change(
        GEMINI_PREFERENCES_CHANGE_KEY,
        _on_preferences_changed
    )


def _unwatch_preferences():
    """Remove the preferences callback registered by this plugin instance."""
    settings = sublime.load_settings("Preferences.sublime-settings")
    settings.clear_on_change(GEMINI_PREFERENCES_CHANGE_KEY)


def _on_preferences_changed():
    """Schedule a marker redraw after package resources are rebuilt."""
    global preferences_redraw_scheduled

    if preferences_redraw_scheduled:
        return

    preferences_redraw_scheduled = True
    LOG.info("Scheduling Gemini input marker redraw after preferences change")
    sublime.set_timeout(_refresh_input_phantoms, 500)


def _refresh_input_phantoms():
    """Force input markers to redraw after package resource reload."""
    global preferences_redraw_scheduled

    try:
        for window_id, session in list(gemini_clients.items()):
            session.input_marker.update()
    finally:
        preferences_redraw_scheduled = False


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
        custom_cwd = window.settings().get(GEMINI_ACTIVE_WORKSPACE)
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
            self.view.settings().set("is_loading", True)
            self.frame_index = 0
            self.region_provider = region
            self._update_animation()

    def stop(self):
        """Stop the loading animation and clear the phantom."""
        self.is_loading = False
        self.view.settings().set("is_loading", False)
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


class InputPromptMarker:
    """Render the live ❯ prompt without storing it in the chat buffer."""
    HTML = (
        "<body id='gemini-input-marker' style='margin:0;padding:0'>"
        "<span style='color:var(--foreground);padding-right:0.2em'>❯</span>"
        "</body>"
    )

    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "gemini_input_marker")

    def update(self):
        anchors = self.view.get_regions(GEMINI_INPUT_ANCHOR)
        if not anchors or anchors[0].empty():
            self.clear()
            return

        start = anchors[0].end()
        if start > self.view.size():
            self.clear()
            return
        phantom = sublime.Phantom(
            sublime.Region(start, start),
            self.HTML,
            sublime.LAYOUT_INLINE
        )
        self.phantom_set.update([phantom])

    def clear(self):
        self.phantom_set.update([])


class ChatSession:
    """
    Manages the state and UI for a single Gemini chat session.
    """
    def __init__(self, window, view, initial_msg="", send_immediate=False, cwd=None):
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
        self.input_marker = InputPromptMarker(self.chat_view)
        if self.chat_view.settings().has(GEMINI_INPUT_START):
            self.input_marker.update()

        # Message on chat startup
        self.initial_msg = initial_msg

        # History state
        self.history = []
        self.history_index = 0
        self.history_stash = ""

        # Thought state
        # List of {"text": str, "expanded": bool, "region_key": str}
        self.thought_blocks = []
        self.current_thought_text = ""
        self.current_thought_id = 0
        self.current_msgid = 0
        self.thought_phantom_set = sublime.PhantomSet(self.chat_view, "gemini_thoughts")
        self.send_immediate = send_immediate
        self.last_is_tool = False
        self.is_startup = True
        self.is_reconnecting = self.chat_view.settings().has(GEMINI_INPUT_START)

        self.cwd = cwd or get_best_dir(self.chat_view)
        session_id = self.chat_view.settings().get(GEMINI_SESSION_ID)
        self.agent_name = get_current_agent(self.window, self.chat_view)
        self.chat_view.settings().set(GEMINI_AGENT, self.agent_name)
        self.update_chat_title()

        # Initialize artifact manager for Antigravity
        if self.agent_name == AGENT_ANTIGRAVITY:
            self.artifact_manager = AntigravityArtifactManager(
                self.chat_view, self.window, input_start_fn=get_input_start
            )
        else:
            self.artifact_manager = None

        # Create the agent client
        self.client = self._create_client(
            session_id=session_id,
            ignore_history=bool(session_id)
        )

    def update_chat_title(self):
        """Update the chat view tab title based on the active agent."""
        update_chat_title(self.chat_view, self.agent_name)

    def _create_client(self, session_id=None, ignore_history=False):
        callbacks = {
            'on_message': self.on_message,
            'on_user_message': self.on_user_message,
            'on_error': self.on_error,
            'on_stop': self.on_stop,
            'on_permission_request': self.on_permission_request,
            'on_session_ready': self.on_session_ready,
            'on_exit': self.on_exit,
            'on_thought': self.on_thought,
            'on_tool_call': self.on_tool_call
        }
        if self.agent_name == AGENT_ANTIGRAVITY:
            desired_model = get_current_model(self.window, self.chat_view, agent=AGENT_ANTIGRAVITY)
            desired_effort = get_current_effort(self.window, self.chat_view, agent=AGENT_ANTIGRAVITY)
            approve_mode = self.window.settings().get(GEMINI_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
            skip_permissions = get_antigravity_skip_permissions(self.window, self.chat_view)
            return AntigravityClient(
                callbacks=callbacks,
                cwd=self.cwd,
                session_id=session_id,
                ignore_history=ignore_history,
                model=desired_model,
                approve_mode=approve_mode,
                effort=desired_effort,
                skip_permissions=skip_permissions
            )
        else:
            return GeminiClient(
                callbacks=callbacks,
                cwd=self.cwd,
                session_id=session_id,
                ignore_history=ignore_history
            )

    def _start_client(self):
        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        extra_env = settings.get("env", {})
        if self.agent_name == AGENT_ANTIGRAVITY:
            cmd = settings.get("antigravity_command", "").strip() or None
        else:
            cmd = settings.get("gemini_command", "gemini").strip() or None
        self.start(
            settings.get("api_key", "").strip(),
            cmd,
            extra_env
        )

    def switch_agent(self, new_agent):
        """Switch the current chat session to another agent provider."""
        if self.agent_name == new_agent:
            sublime.status_message(f"Already using {new_agent}")
            return

        LOG.info("Switching agent from %s to %s in %s", self.agent_name, new_agent, self.cwd)
        self.agent_name = new_agent
        self.chat_view.settings().set(GEMINI_AGENT, new_agent)
        self.update_chat_title()

        # Stop current process and animations
        self.stop()

        # Clear UI phantoms
        self.phantom_set.update([])
        self.thought_phantom_set.update([])
        for block in self.thought_blocks:
            self.chat_view.erase_regions(block["region_key"])

        # Reset state
        self.pending_permissions = {}
        self.shown_tool_calls = set()
        self.thought_blocks = []
        self.current_thought_text = ""
        self.current_thought_id = 0
        self.current_msgid = 0
        self.pending_diff = {}

        # Clear session ID so new agent starts fresh
        self.chat_view.settings().erase(GEMINI_SESSION_ID)

        # Update artifact manager
        if new_agent == AGENT_ANTIGRAVITY:
            if not self.artifact_manager:
                self.artifact_manager = AntigravityArtifactManager(
                    self.chat_view, self.window, input_start_fn=get_input_start
                )
        else:
            if self.artifact_manager:
                self.artifact_manager.clear()
                self.artifact_manager = None

        display_name = "Antigravity (agy)" if new_agent == AGENT_ANTIGRAVITY else "Gemini CLI"
        self.chat_view.run_command("gemini_chat_append", {"text": f"\n\nSwitched agent to {display_name} in {self.cwd}...\n\n"})

        # Reset client with NEW session
        self.client = self._create_client(session_id=None, ignore_history=True)
        self._start_client()

    def clear_session(self):
        """Clears the current chat session by restarting the agent."""
        LOG.info("Clearing chat session in %s", self.cwd)

        # Stop current process and animations
        self.stop()

        # Clear UI phantoms
        self.phantom_set.update([])
        self.thought_phantom_set.update([])
        for block in self.thought_blocks:
            self.chat_view.erase_regions(block["region_key"])

        # Reset state
        self.pending_permissions = {}
        self.shown_tool_calls = set()
        self.thought_blocks = []
        self.current_thought_text = ""
        self.current_thought_id = 0
        self.current_msgid = 0
        self.pending_diff = {}

        if self.artifact_manager:
            self.artifact_manager.clear()

        # Clear session ID to ensure a fresh session is started
        self.chat_view.settings().erase(GEMINI_SESSION_ID)

        agent_title = "Antigravity" if self.agent_name == AGENT_ANTIGRAVITY else "Gemini CLI"
        self.chat_view.run_command("gemini_chat_append", {"text": f"\n\n{agent_title} session reset in {self.cwd}...\n\n"})

        # Reset client with NEW session (session_id=None) but same cwd
        self.client = self._create_client(session_id=None, ignore_history=True)

        # Start again
        self._start_client()

    def switch_workspace(self, new_cwd):
        """Switch the session to a new working directory."""
        if new_cwd == self.cwd:
            return

        LOG.info("Switching workspace to new cwd: %s", new_cwd)
        self.cwd = new_cwd

        # Stop current process
        self.stop()

        # Clear session ID to ensure a fresh session is started in the new directory
        self.chat_view.settings().erase(GEMINI_SESSION_ID)

        self.chat_view.run_command("gemini_chat_append", {"text": f"\n\nSwitching Workspace to: {new_cwd}\n\n"})

        # Reset client with NEW session (session_id=None) but same cwd
        self.client = self._create_client(session_id=None, ignore_history=True)

        # Start again
        self._start_client()

    def resume_session(self, session_id):
        """Resume a previous Antigravity session by reconnecting to the specified session_id."""
        LOG.info("Resuming session %s in %s", session_id, self.cwd)

        # Stop current process and animations
        self.stop()

        # Clear UI phantoms
        self.phantom_set.update([])
        self.thought_phantom_set.update([])
        for block in self.thought_blocks:
            self.chat_view.erase_regions(block["region_key"])

        # Reset state
        self.pending_permissions = {}
        self.shown_tool_calls = set()
        self.thought_blocks = []
        self.current_thought_text = ""
        self.current_thought_id = 0
        self.current_msgid = 0
        self.pending_diff = {}

        self.agent_name = AGENT_ANTIGRAVITY
        self.chat_view.settings().set(GEMINI_AGENT, AGENT_ANTIGRAVITY)
        self.chat_view.settings().set(GEMINI_SESSION_ID, session_id)
        self.update_chat_title()
        self.is_startup = False
        self.is_reconnecting = True

        if not self.artifact_manager:
            self.artifact_manager = AntigravityArtifactManager(
                self.chat_view, self.window, input_start_fn=get_input_start
            )
        else:
            self.artifact_manager.clear()

        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        history_limit = settings.get("session_history_limit", 50)
        session_info = get_antigravity_session_tail(session_id, self.cwd, history_limit=history_limit)

        banner = f"\n\n[Resuming Antigravity session {session_id[:8]}...]\n\n"
        if not self.chat_view.settings().has(GEMINI_INPUT_START):
            if self.cwd:
                self.chat_view.run_command("append", {"characters": f"cwd: {self.cwd}\n"})
            self.chat_view.run_command("append", {"characters": banner.lstrip()})
        else:
            self.chat_view.run_command("gemini_chat_append", {"text": banner})

        if session_info and session_info.get("turns"):
            for turn in session_info["turns"]:
                prompt = turn.get("prompt")
                if prompt:
                    if not self.chat_view.settings().has(GEMINI_INPUT_START):
                        self.chat_view.run_command("append", {"characters": PROMPT_PREFIX + prompt + "\n\n"})
                    else:
                        self.chat_view.run_command("gemini_chat_append", {"text": PROMPT_PREFIX + prompt + "\n\n"})
                response = turn.get("response")
                if response:
                    if not self.chat_view.settings().has(GEMINI_INPUT_START):
                        self.chat_view.run_command("append", {"characters": response + "\n\n"})
                    else:
                        self.chat_view.run_command("gemini_chat_append", {"text": response + "\n\n"})

        if not self.chat_view.settings().has(GEMINI_INPUT_START):
            self.chat_view.run_command("gemini_chat_prompt", {"text": ""})

        # Sync artifacts for this resumed conversation
        if self.artifact_manager:
            self.artifact_manager.sync_and_render(session_id)

        # Reset and start client
        self.client = self._create_client(session_id=session_id, ignore_history=True)
        self._start_client()

    def set_initial_msg(self, text):
        """Set or append text to initial_msg."""
        if self.initial_msg:
            self.initial_msg += " " + text
        else:
            self.initial_msg = text

    def loading_region(self):
        """Get the region where the loading animation should be displayed."""
        input_start = get_input_start(self.chat_view)
        return sublime.Region(input_start - 1, input_start)

    def start(self, api_key, gemini_command=None, extra_env=None):
        env = dict(extra_env) if extra_env else {}
        settings = sublime.load_settings("GeminiCLI.sublime-settings")
        if settings.get("share_workspace_folders", True):
            folders = self.window.folders()
            if folders:
                env["GEMINI_CLI_IDE_WORKSPACE_PATH"] = os.pathsep.join(folders)

        self.client.start(api_key, gemini_command, env)
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
        self.loading_animation.start(self.loading_region)
        self.last_is_tool = False
        # Keep the merged thought block at the start of this response.
        anchor_start = get_input_start(self.chat_view) - 4
        region_key = "gemini_thought_anchor_%d" % len(self.thought_blocks)
        self.chat_view.add_regions(
            region_key,
            [sublime.Region(anchor_start, anchor_start + 1)],
            flags=sublime.HIDDEN
        )
        prompt_id = self.client.send_input(user_input)
        self.current_msgid = prompt_id

    def on_user_message(self, text):
        """Handle user message chunks from Gemini (e.g. during history stream)."""
        sublime.set_timeout(lambda: self._on_user_message_process(text), 0)

    def _on_user_message_process(self, text):
        # We append a prompt prefix and the text
        if not self.last_is_tool:
            self.chat_view.run_command("gemini_chat_append", {"text": "\n"})
        self.chat_view.run_command("gemini_chat_append", {"text": PROMPT_PREFIX + text + "\n"})
        self.last_is_tool = True

    def on_message(self, text):
        """Handle message chunks from Gemini."""
        # Dispatch to main thread to ensure thread safety for UI updates and state modification
        sublime.set_timeout(lambda: self._on_message_process(text), 0)

    def _ensure_session_id_saved(self):
        """Save session ID to view settings for persistence if not already set."""
        if self.client and self.client.session_id:
            if self.chat_view.settings().get(GEMINI_SESSION_ID) != self.client.session_id:
                self.chat_view.settings().set(GEMINI_SESSION_ID, self.client.session_id)

    def _on_message_process(self, text):
        self._ensure_session_id_saved()

        # Ensure loading animation is active
        self.loading_animation.start(self.loading_region)

        if self.last_is_tool:
            text = "\n" + text

        self.chat_view.run_command("gemini_chat_append", {"text": text})
        self.last_is_tool = False

    def on_error(self, message):
        """Handle error messages."""
        def _on_error_process():
            self.loading_animation.stop()
            self.chat_view.run_command("gemini_chat_append", {"text": "\nError: " + message + "\n"})
            self.last_is_tool = False
        sublime.set_timeout(_on_error_process, 0)

    def on_stop(self, msg_id, stop_text):
        """Handle stop signal from Gemini / Antigravity."""
        def _on_stop_process():
            self.loading_animation.stop()
            # Clear interactive UI elements since the turn is over
            self.phantom_set.update([])
            self.pending_permissions = {}

            # Sync and render any plan / walkthrough artifacts for Antigravity
            if self.artifact_manager and self.client:
                session_id = getattr(self.client, "session_id", None) or self.chat_view.settings().get(GEMINI_SESSION_ID)
                self.artifact_manager.sync_and_render(session_id)

            if stop_text == "cancelled":
                self.chat_view.run_command("gemini_chat_append", {"text": "\n[Interrupted]\n\n"})
            else:
                self.chat_view.run_command("gemini_chat_append", {"text": "\n\n"})
            self.last_is_tool = False
        sublime.set_timeout(_on_stop_process, 0)
        LOG.info("prompt %s completed: %s", msg_id, stop_text)

    def on_session_ready(self):
        """Handle session ready notification."""
        self.loading_animation.stop()

        # Ensure the desired model and effort are applied
        desired_model = get_current_model(self.window, self.chat_view, agent=self.agent_name)
        desired_effort = get_current_effort(self.window, self.chat_view, agent=self.agent_name)
        if desired_model and desired_model != "default":
            model_changed = getattr(self.client, "current_model_id", None) != desired_model
            effort_changed = hasattr(self.client, "current_effort") and getattr(self.client, "current_effort", None) != desired_effort
            if model_changed or effort_changed:
                self.client.agent_session_set_model(desired_model, effort=desired_effort)
                self.client.current_model_id = desired_model
                if hasattr(self.client, "current_effort"):
                    self.client.current_effort = desired_effort
        elif hasattr(self.client, "current_effort") and getattr(self.client, "current_effort", None) != desired_effort:
            if hasattr(self.client, "agent_session_set_effort"):
                self.client.agent_session_set_effort(desired_effort)
            self.client.current_effort = desired_effort

        # Only show the welcome text when initializing a brand-new chat view.
        if self.is_reconnecting or not self.is_startup:
            if self.is_startup:
                self.is_startup = False
                if self.initial_msg:
                    self.chat_view.run_command("append", {"characters": self.initial_msg + " "})
                    if self.send_immediate:
                        self.send_immediate = False
                        self.chat_view.run_command("gemini_send_input")
                    self.initial_msg = ""
            else:
                if self.initial_msg:
                    # Append initial msg to the existing prompt
                    self.chat_view.run_command("append", {"characters": self.initial_msg + " "})
                    if self.send_immediate:
                        self.send_immediate = False
                        self.chat_view.run_command("gemini_send_input")
                    self.initial_msg = ""
            return

        self.is_startup = False
        shortcut = "Command+Enter" if sublime.platform() == "osx" else "Control+Enter"
        if self.agent_name == AGENT_ANTIGRAVITY:
            model = (
                getattr(self.client, "current_model_id", None)
                or get_current_model(self.window, self.chat_view, AGENT_ANTIGRAVITY)
            )
            effort = (
                getattr(self.client, "current_effort", None)
                or get_current_effort(self.window, self.chat_view, AGENT_ANTIGRAVITY)
            )
            if model and model != "default":
                display = f"{model}:{effort}" if effort else model
                header = f"Interactive Antigravity ({display})"
            else:
                header = "Interactive Antigravity"
        else:
            model = get_current_model(self.window, self.chat_view, AGENT_GEMINI)
            if model and model != "default":
                header = f"Interactive Gemini CLI ({model})"
            else:
                header = "Interactive Gemini CLI"
        welcome_text = f"{header}\nType your message and press {shortcut} to send.\n"
        self.chat_view.run_command("append", {"characters": welcome_text})
        if self.initial_msg:
            self.chat_view.run_command("gemini_chat_prompt", {"text": self.initial_msg})
            if self.send_immediate:
                self.send_immediate = False
                self.chat_view.run_command("gemini_send_input")
            self.initial_msg = ""
        else:
            self.chat_view.run_command("gemini_chat_prompt", {"text": ""})

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
        self._ensure_session_id_saved()

        # Ensure loading animation is active
        self.loading_animation.start(self.loading_region)

        tool_id = tool_call.get("toolCallId")
        if tool_id and tool_id in self.shown_tool_calls:
            return
        if tool_id:
            self.shown_tool_calls.add(tool_id)

        tool_kind = tool_call.get("kind", "tool")
        tool_title = tool_call.get("title", tool_call.get("name", ""))
        tool_name = tool_call.get("name", "")
        params = tool_call.get("parameters", tool_call.get("args", {}))
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}

        # Normalize file tool kinds and target file paths
        if tool_kind == "tool" and tool_name:
            if tool_name in ("view_file", "read_file", "Read", "ViewFile"):
                tool_kind = "read"
                if not tool_title or tool_title == tool_name:
                    tool_title = params.get("AbsolutePath") or params.get("TargetFile") or params.get("file_path") or ""
            elif tool_name in (
                "write_to_file", "replace_file_content", "edit_file",
                "Edit", "Write", "WriteFile", "EditFile"
            ):
                tool_kind = "edit"
                if not tool_title or tool_title == tool_name:
                    tool_title = params.get("TargetFile") or params.get("AbsolutePath") or params.get("file_path") or ""

        if tool_kind == "execute" and tool_title:
            # Remove content within [ ] from the execution title
            tool_title = re.sub(r'\s*\[.*?\]', '', tool_title)

        formatted_title = f"⏺ {tool_kind.capitalize()}"
        if tool_title:
            if "\n" in tool_title:
                title_lines = tool_title.split("\n", 1)
                first_line = title_lines[0]
                rest_of_code = title_lines[1] if len(title_lines) > 1 else ""
                # Indent the rest of the code by 4 spaces to use Markdown's indent-based code block
                indented_code = "\n".join("    " + l for l in rest_of_code.split("\n"))
                formatted_title = f"⏺ {tool_kind.capitalize()} {first_line}\n\n{indented_code}"
            else:
                formatted_title = f"⏺ {tool_kind.capitalize()} {tool_title}"

        # Determine prefix based on previous output type
        view = self.chat_view
        prefix = ""
        insert_pos = get_input_start(view, 0) - 1

        if insert_pos > 0:
            # Read up to 2 characters before the insertion point
            start_check = max(0, insert_pos - 2)
            last_chars = view.substr(sublime.Region(start_check, insert_pos))
            last_char = last_chars[-1] if last_chars else ""

            if last_char != "\n":
                prefix = "\n"

            if not self.last_is_tool and last_chars != "\n\n":
                # Ensure a blank line if previous wasn't a tool and no blank line exists
                prefix += "\n"

        selected_text = f"{prefix}{formatted_title}\n"
        view.run_command("gemini_chat_append", {"text": selected_text})
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
        """Handle tool call update from Gemini or Antigravity."""
        # Ensure loading animation is active when a tool call arrives
        sublime.set_timeout(lambda: self.loading_animation.start(self.loading_region), 0)

        if self.artifact_manager:
            self.artifact_manager.record_from_tool_call(tool_call)
            if tool_call.get("status") != "in_progress" and self.artifact_manager.pending_render:
                sublime.set_timeout(self.artifact_manager.render_pending_artifacts, 0)
        if tool_call.get("status") == "in_progress":
            sublime.set_timeout(lambda: self._output_tool_call_text(tool_call), 0)

    def _on_thought_process(self, text):
        self._ensure_session_id_saved()

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

            region_key = "gemini_thought_anchor_%d" % len(self.thought_blocks)
            self.thought_blocks.append({
                "text": text,
                "expanded": False,
                "region_key": region_key
            })
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

            anchors = self.chat_view.get_regions(block["region_key"])
            if not anchors:
                continue
            anchor_pos = anchors[0].end()
            region = sublime.Region(anchor_pos, anchor_pos)
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
        def _on_exit_process():
            self.loading_animation.stop()
            self.phantom_set.update([])
            self.thought_phantom_set.update([])
            sublime.status_message("Gemini CLI session ended")
        sublime.set_timeout(_on_exit_process, 0)

    def show_permission_phantom(self, phantom_id, options, tool_call):
        """Display a phantom with permission options."""
        html = self.create_permission_phantom_html(phantom_id, options, tool_call)
        input_start = get_input_start(self.chat_view)
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
    def run(self, initial_msg="", send_immediate=False, view_id=None, cwd=None, session_id=None):
        # Check if a client already exists for this window
        window_id = self.window.id()
        if window_id in gemini_clients:
            # Try to find and focus existing chat view
            for view in self.window.views():
                if view.settings().get(GEMINI_CHAT_VIEW, False):
                    self.window.focus_view(view)
                    if session_id:
                        gemini_clients[window_id].resume_session(session_id)
                    else:
                        sublime.status_message("Gemini: Already active in this window.")
                    return
            # If client exists but no view found, clean up
            del gemini_clients[window_id]

        chat_view = None
        if view_id is not None:
            chat_view = sublime.View(view_id)
            if not chat_view.is_valid() or not chat_view.settings().get(GEMINI_CHAT_VIEW, False):
                chat_view = None

        if chat_view:
            agent = get_current_agent(self.window, chat_view)
            update_chat_title(chat_view, agent)
            agent_title = "Antigravity" if agent == AGENT_ANTIGRAVITY else "Gemini CLI"
            chat_view.run_command(
                "gemini_chat_append",
                {"text": f"Reconnecting to {agent_title}\n"}
            )
        else:
            # Create a new view to display the result
            if session_id:
                agent = AGENT_ANTIGRAVITY
            else:
                agent = get_current_agent(self.window)
            agent_title = "Antigravity" if agent == AGENT_ANTIGRAVITY else "Gemini CLI"
            chat_view = self.window.new_file()
            update_chat_title(chat_view, agent)
            chat_view.set_scratch(True)
            chat_view.set_syntax_file("Packages/GeminiCLI/GeminiChat.sublime-syntax")
            chat_view.settings().set("draw_minimap", False)
            chat_view.settings().set("line_numbers", False)
            chat_view.settings().set("word_wrap", True)
            chat_view.settings().set(GEMINI_CHAT_VIEW, True)
            chat_view.settings().set(GEMINI_AGENT, agent)
            if session_id:
                chat_view.settings().set(GEMINI_SESSION_ID, session_id)
            else:
                chat_view.run_command("append", {"characters": f"Starting {agent_title}...\n"})

        resolved_cwd = cwd or get_best_dir(chat_view)
        if resolved_cwd and not session_id:
            if chat_view.settings().has(GEMINI_INPUT_START):
                chat_view.run_command(
                    "gemini_chat_append",
                    {"text": "cwd: %s\n" % resolved_cwd}
                )
            else:
                chat_view.run_command("append", {"characters": "cwd: %s\n" % resolved_cwd})

        # Create and start the ChatSession
        session = ChatSession(self.window, chat_view, initial_msg=initial_msg, send_immediate=send_immediate, cwd=resolved_cwd)
        gemini_clients[window_id] = session

        if session_id:
            session.resume_session(session_id)
        else:
            session._start_client()


class GeminiInterruptCommand(sublime_plugin.WindowCommand):
    """
    Interrupts the current Gemini conversation.
    """
    def run(self):
        window_id = self.window.id()
        if window_id not in gemini_clients:
            return

        session = gemini_clients[window_id]
        client = getattr(session, "client", None)
        if client and getattr(client, "inited", False) and (session.loading_animation.is_loading or session.pending_permissions):
            sublime.status_message("Interrupting agent...")
            client.agent_session_cancel()

    def is_enabled(self):
        window_id = self.window.id()
        if window_id not in gemini_clients:
            return False
        session = gemini_clients[window_id]
        client = getattr(session, "client", None)
        return bool(client and getattr(client, "inited", False) and (session.loading_animation.is_loading or session.pending_permissions))


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

        editable_start = input_editable_start(self.view)
        input_region = sublime.Region(editable_start, self.view.size())
        user_input = self.view.substr(input_region).strip()

        if not user_input:
            return

        session = gemini_clients[window_id]
        if not session.history or session.history[-1] != user_input:
            session.history.append(user_input)
        session.history_index = len(session.history)
        session.history_stash = ""

        sublime.status_message("Sending message...")

        # Keep the prompt marker in the transcript while the live marker moves
        # to the next input line.
        self.view.insert(edit, editable_start, "❯ ")

        # Show input text and next prompt (simulated local echo/confirmation)
        self.view.run_command("gemini_chat_prompt", {"text": ""})

        # Send to client
        gemini_clients[window_id].send_input(user_input)
        LOG.info("User enter prompt %s", user_input)


class GeminiHistoryUpCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in gemini_clients:
            return

        session = gemini_clients[window.id()]
        editable_start = input_editable_start(self.view)

        # History navigation
        if session.history_index == len(session.history):
            # Stash current input
            current_input_region = sublime.Region(editable_start, self.view.size())
            session.history_stash = self.view.substr(current_input_region)

        if session.history_index > 0:
            session.history_index -= 1
            self._replace_input(edit, session.history[session.history_index], editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())


class GeminiHistoryDownCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in gemini_clients:
            return

        session = gemini_clients[window.id()]

        # History navigation
        if session.history_index < len(session.history):
            session.history_index += 1

            text_to_show = ""
            if session.history_index == len(session.history):
                text_to_show = session.history_stash
            else:
                text_to_show = session.history[session.history_index]

            editable_start = input_editable_start(self.view)
            self._replace_input(edit, text_to_show, editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())


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
            session = gemini_clients[window_id]
            if session.chat_view.id() == view.id():
                session.input_marker.update()
            return

        # Check if this window contains an orphaned chat view
        for v in window.views():
            if v.settings().get(GEMINI_CHAT_VIEW, False):
                _reconnect_chat_view(v)
                break

    def on_close(self, view):
        """
        Cleanup session when the chat view is closed.
        """
        if view.settings().get(GEMINI_CHAT_VIEW, False) or view.name() in CHAT_VIEW_NAMES:
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
        if not view.settings().get(GEMINI_CHAT_VIEW, False) and view.name() not in CHAT_VIEW_NAMES:
            return
        if not view.settings().has(GEMINI_INPUT_START):
            return

        editable_start = input_editable_start(view)

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
        if not view.settings().get(GEMINI_CHAT_VIEW, False) and view.name() not in CHAT_VIEW_NAMES:
            return None

        # Double-click on a Markdown file link -> open file and jump to line
        if (
            command_name == "drag_select"
            and args
            and not args.get("extend", False)
            and not args.get("additive", False)
        ):
            is_word_select = args.get("by") == "words"
            if is_word_select:
                event = args.get("event", {})
                x, y = event.get("x"), event.get("y")
                if x is not None and y is not None:
                    click_point = view.window_to_text((x, y))
                    if click_point is not None:
                        window = view.window() or sublime.active_window()
                        if window:
                            # 1. Double-click on Markdown file link ([...](path#L10) or file://)
                            if open_local_file_link(
                                view, click_point, window=window
                            ):
                                return ("noop", {})

                            session = gemini_clients.get(window.id())
                            cwd = getattr(session, "cwd", None)

                            # 2. Double-click on tool target file (⏺ Read /path/to/file) or diff hunk (@@ ... @@)
                            if open_tool_file_link(
                                view, click_point, cwd=cwd, window=window
                            ):
                                return ("noop", {})

                            # 3. Double-click on Antigravity artifact (▣ Plan: ...)
                            if session and session.artifact_manager:
                                if session.artifact_manager.open_artifact_at(click_point):
                                    return ("noop", {})

        editable_start = input_editable_start(view)

        if command_name == "move" and args and args.get("by") == "lines":
            # Don't intercept if auto-complete is active, so user can select items
            if not view.is_auto_complete_visible():
                is_up = not args.get("forward", True)
                if len(view.sel()) > 0:
                    sel = view.sel()[0]
                    if sel.empty():
                        if is_up:
                            row_sel, _ = view.rowcol(sel.begin())
                            row_start, _ = view.rowcol(editable_start)
                            if row_sel == row_start:
                                return ("gemini_history_up", {})
                        else:
                            row_sel, _ = view.rowcol(sel.end())
                            row_last, _ = view.rowcol(view.size())
                            if row_sel == row_last:
                                return ("gemini_history_down", {})

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
        Provide filename and directory completions when typing '@' in the prompt area.
        Delegates to AutoComplete for path-segmented completions with multi-level
        directory drilling and multi-workspace routing.
        """
        if not view.settings().get(GEMINI_CHAT_VIEW, False):
            return None

        editable_start = input_editable_start(view)
        if editable_start is None:
            return None

        return AutoComplete.generate_completions(
            view=view,
            locations=locations,
            editable_start=editable_start,
            chat_view_flag=GEMINI_CHAT_VIEW,
            chat_workspace_key=GEMINI_ACTIVE_WORKSPACE,
        )

    def on_modified_async(self, view):
        """
        Trigger autocompletion immediately when '@' or a directory '/' is typed.
        """
        if not view.settings().get(GEMINI_CHAT_VIEW, False):
            return

        sel = view.sel()
        if not sel:
            return

        pos = sel[0].begin()
        if pos <= 0:
            return

        editable_start = input_editable_start(view)
        if editable_start is None or pos < editable_start:
            return

        last_char = view.substr(pos - 1)
        if last_char == '@':
            # Run auto_complete command
            view.run_command("auto_complete", {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False,
            })
        elif last_char == '/':
            AutoComplete.check_cascade_trigger(view, editable_start)


class GeminiChatAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        if (not self.view.settings().has(GEMINI_INPUT_START)
                and not self.view.get_regions(GEMINI_INPUT_ANCHOR)):
            self.view.insert(edit, self.view.size(), text)
            self.view.show(self.view.size())
            return
        insert_at = get_input_start(self.view, 0) - 1
        inserted = self.view.insert(edit, insert_at, text)
        set_input_start(self.view, insert_at + inserted + 1)
        self.view.show(self.view.size())


class GeminiChatPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        # The final newline is the moving anchor for the live input line.
        self.view.insert(edit, self.view.size(), "\n\n\n\n\n")
        set_input_start(self.view, self.view.size() - 1)

        if text:
            self.view.insert(edit, self.view.size(), text + " ")
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)

        window = self.view.window()
        if window and window.id() in gemini_clients:
            gemini_clients[window.id()].input_marker.update()


class GeminiAddContextCommand(sublime_plugin.WindowCommand):
    """
    Command to start or focus Gemini chat with optional context.
    File selections are added as @-tags; selected chat history is quoted into
    the current prompt.
    """
    def run(self):
        view = self.window.active_view()
        file_path = None
        context_tag = ""
        insert_text = ""

        if view and view.settings().get(GEMINI_CHAT_VIEW, False):
            # Capture the history selection before focus/cursor handling snaps
            # it back to the editable prompt. Clip selections at the prompt
            # boundary so current input is never quoted as chat history.
            editable_start = input_editable_start(view)
            selected_parts = []
            for selection in view.sel():
                history_end = min(selection.end(), editable_start)
                if not selection.empty() and selection.begin() < history_end:
                    selected_parts.append(view.substr(sublime.Region(
                        selection.begin(), history_end
                    )))

            selected = "\n".join(selected_parts)
            if not selected.strip():
                return

            context_tag = "\n".join(
                "> " + line for line in selected.splitlines()
            )
            input_text = view.substr(sublime.Region(
                editable_start, view.size()
            ))
            insert_text = context_tag + "\n\n"
            trailing = len(input_text) - len(input_text.rstrip("\n"))
            # Keep an empty prompt compact; otherwise leave one complete blank
            # line between existing input and the quote.
            required_breaks = 1 if not input_text else max(0, 2 - trailing)
            insert_text = "\n" * required_breaks + insert_text
        else:
            file_path = view.file_name() if view else None

        if not insert_text and file_path:
            # Get line numbers (1-based)
            sel = view.sel()[0]
            row_start, _ = view.rowcol(sel.begin())
            row_end, _ = view.rowcol(sel.end())

            # Format as @file_path#L(A)-(B)
            # Handle single line selection vs range
            if row_start == row_end:
                context_tag = f"@{file_path}#L{row_start + 1}"
            else:
                context_tag = f"@{file_path}#L{row_start + 1}-{row_end + 1}"
            insert_text = context_tag + " "

        # Find or create Gemini chat view
        chat_view = None
        for v in self.window.views():
            if v.settings().get(GEMINI_CHAT_VIEW, False):
                chat_view = v
                break

        if not chat_view:
            # A non-file view has no context tag, but should still start chat.
            self.window.run_command("gemini_cli", {"initial_msg": context_tag})
        else:
            self.window.focus_view(chat_view)
            if not insert_text:
                return

            # A history selection is allowed for copying/quoting, but insert
            # commands there are blocked to protect prior messages. Move that
            # selection to the prompt before inserting the captured context.
            editable_start = input_editable_start(chat_view)
            selections = list(chat_view.sel())
            if not (
                len(selections) == 1
                and selections[0].begin() >= editable_start
            ):
                chat_view.sel().clear()
                chat_view.sel().add(sublime.Region(chat_view.size()))

            chat_view.run_command("insert", {"characters": insert_text})
            chat_view.show(chat_view.sel()[0].end())


class GeminiAddFileCommand(sublime_plugin.WindowCommand):
    """
    Command to add file or directory reference to the Gemini chat prompt.
    Works from tab context menu and sidebar.
    If cwd is not set, uses the top-level directory of the selected item as cwd.
    """
    def run(self, files=None, dirs=None):
        window = self.window
        if not window:
            return

        # Get file path from either files/dirs parameter (sidebar) or active view (tab)
        selected_path = None
        if files and len(files) > 0:
            selected_path = files[0]
        elif dirs and len(dirs) > 0:
            selected_path = dirs[0]
        else:
            view = window.active_view()
            if view:
                selected_path = view.file_name()

        if not selected_path:
            return

        context_tag = f"@{selected_path}"

        # Find or create Gemini chat view
        chat_view = None
        for v in window.views():
            if v.settings().get(GEMINI_CHAT_VIEW, False):
                chat_view = v
                break

        if not chat_view:
            top_level_dir = self._get_top_level_dir(window, selected_path)
            # If no chat view, create one and pass the context tag and calculated cwd immediately
            kwargs = {"initial_msg": context_tag}
            if top_level_dir:
                kwargs["cwd"] = top_level_dir
            window.run_command("gemini_cli", kwargs)
        else:
            window.focus_view(chat_view)
            self._insert_tag(chat_view, context_tag)

    def _get_top_level_dir(self, window, selected_path):
        """
        Finds the top-level directory for a given path from the window's folders.
        Returns the folder if found, otherwise returns the path itself (if a dir)
        or its parent directory.
        """
        if not selected_path:
            return None

        folders = window.folders()
        if folders:
            for folder in folders:
                if selected_path == folder or selected_path.startswith(folder + os.sep):
                    return folder

        if os.path.isdir(selected_path):
            return selected_path
        return os.path.dirname(selected_path)

    def _insert_tag(self, chat_view, context_tag):
        # Insert at the end of the view (current prompt area)
        chat_view.run_command("insert", {"characters": context_tag + " "})
        # Move cursor to end
        chat_view.sel().clear()
        chat_view.sel().add(sublime.Region(chat_view.size()))
        chat_view.show(chat_view.size())

    def is_visible(self, files=None, dirs=None):
        if files is not None or dirs is not None:
            return bool(files or dirs)
        return True


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

        context_tag = f"@{file_path}"

        # Find or create Gemini chat view
        chat_view = None
        for v in window.views():
            if v.settings().get(GEMINI_CHAT_VIEW, False):
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
        return not self.view.settings().get(GEMINI_CHAT_VIEW, False)


class GeminiPromptCommand(sublime_plugin.WindowCommand):
    """Collect a quick prompt in an input panel and submit it."""

    def run(self, gemini_prompt=None):
        # Capture editor context before the input panel takes focus. A file is
        # attached only when the user has made a non-empty selection.
        source_view = self.window.active_view()
        context_tags = self._selected_context_tags(source_view)

        # Keep the argument form available for programmatic callers. Invoking
        # the command from the command palette opens a separate, multiline
        # input panel instead of using a TextInputHandler inside the palette.
        if gemini_prompt is not None:
            self._submit(gemini_prompt, context_tags)
            return

        context_text = " ".join(context_tags)
        initial_text = f"{context_text}\n\n" if context_text else "\n\n"
        panel = self.window.show_input_panel(
            "Message Gemini:",
            initial_text,
            lambda text: self._submit_from_panel(text, context_tags),
            None,
            None,
        )
        panel.settings().set("word_wrap", True)
        panel.settings().set("line_numbers", False)
        panel.settings().set("gutter", False)
        panel.settings().set("scroll_past_end", False)
        caret = len(initial_text) if context_tags else 0
        panel.sel().clear()
        panel.sel().add(sublime.Region(caret))
        panel.show(caret)

    def _selected_context_tags(self, view):
        if not view or view.settings().get(GEMINI_CHAT_VIEW, False):
            return []

        file_path = view.file_name()
        if not file_path:
            return []

        tags = []
        for selection in view.sel():
            if selection.empty():
                continue

            start_row, _ = view.rowcol(selection.begin())
            # Use the final selected character so a selection ending at the
            # next line's first column does not attach an extra line.
            end_row, _ = view.rowcol(selection.end() - 1)
            if start_row == end_row:
                tag = f"@{file_path}#L{start_row + 1}"
            else:
                tag = f"@{file_path}#L{start_row + 1}-{end_row + 1}"
            if tag not in tags:
                tags.append(tag)
        return tags

    def _submit_from_panel(self, gemini_prompt, context_tags):
        # Context is visible and editable, so submit exactly what remains
        # instead of restoring a tag the user deliberately removed.
        content = gemini_prompt.strip()
        if not content:
            return

        message = content
        for tag in context_tags:
            message = message.replace(tag, "", 1)
        if not message.strip():
            return

        self._submit(content)

    def _submit(self, gemini_prompt, context_tags=None):
        gemini_prompt = gemini_prompt.strip()
        if not gemini_prompt:
            return

        missing_tags = [
            tag for tag in (context_tags or []) if tag not in gemini_prompt
        ]
        if missing_tags:
            gemini_prompt = " ".join(missing_tags + [gemini_prompt])

        window_id = self.window.id()
        chat_view = None
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            chat_view = session.chat_view
            # Ensure the prompt is inserted into the live input area rather
            # than a selection left in protected chat history.
            chat_view.sel().clear()
            chat_view.sel().add(sublime.Region(chat_view.size()))
            chat_view.run_command("insert", {"characters": gemini_prompt})
            chat_view.run_command("gemini_send_input")
        else:
            # Start a new session and send immediately
            self.window.run_command("gemini_cli", {
                "initial_msg": gemini_prompt,
                "send_immediate": True
            })
            session = gemini_clients.get(window_id)
            if session:
                chat_view = session.chat_view

        if not chat_view or not chat_view.is_valid():
            return

        # The callback can run while Sublime is still closing the input panel.
        # Focus on the next UI tick so the panel cannot override the chat view.
        def focus_chat_view():
            target_window = chat_view.window()
            if target_window and target_window.id() == self.window.id():
                self.window.focus_view(chat_view)

        sublime.set_timeout(focus_chat_view, 0)


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
            self.window.settings().set(GEMINI_ACTIVE_WORKSPACE, target_dir)
            sublime.status_message(f"Gemini Dir set to: {target_dir}")

            # Switch workspace session if active
            window_id = self.window.id()
            if window_id in gemini_clients:
                session = gemini_clients[window_id]
                session.switch_workspace(target_dir)
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
        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            if hasattr(session, "client") and session.client:
                if hasattr(session.client, "agent_session_set_approve_mode"):
                    skip_perm = get_antigravity_skip_permissions(self.window, getattr(session, "chat_view", None))
                    session.client.agent_session_set_approve_mode(mode, skip_permissions=skip_perm)
                elif hasattr(session.client, "approve_mode"):
                    session.client.approve_mode = mode
        sublime.status_message(f"Approve mode set to: {mode}")

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(GEMINI_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
            return GeminiApproveModeInputHandler(current_mode)
        return None


class GeminiSetEffortListHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_effort=None, selected_model=None, supported_efforts=None, window=None):
        self.current_effort = current_effort
        self.selected_model = selected_model
        self.supported_efforts = supported_efforts
        self.window = window or (sublime.active_window() if sublime else None)

    def name(self):
        return "effort"

    def list_items(self):
        preset_descriptions = {
            "low": "Fast responses with light reasoning",
            "medium": "Balanced speed and reasoning depth",
            "high": "Deep reasoning for complex coding tasks",
        }
        presets = []
        if self.supported_efforts:
            efforts = list(self.supported_efforts)
            if "medium" in efforts:
                efforts.remove("medium")
                efforts.insert(0, "medium")
            for eff in efforts:
                desc = preset_descriptions.get(eff, f"{eff.capitalize()} reasoning depth")
                presets.append((f"{eff}: ({desc})", eff))
        else:
            presets = [
                ("default: (Let agent decide / model default)", ""),
                ("low: (Fast responses with light reasoning)", "low"),
                ("medium: (Balanced speed and reasoning depth)", "medium"),
                ("high: (Deep reasoning for complex coding tasks)", "high"),
            ]

        if self.current_effort:
            for i, item in enumerate(presets):
                if item[1] == self.current_effort:
                    presets.insert(0, presets.pop(i))
                    break

        return presets

    def placeholder(self):
        if self.current_effort:
            return f" ( current: {self.current_effort} ); select reasoning effort"
        return "select reasoning effort"

    def description(self, value, text):
        return f"Reasoning Effort: {value}" if value else "Reasoning Effort: default"


class GeminiSetModelListHandler(sublime_plugin.ListInputHandler):
    def __init__(self, available_models, current_model, window=None):
        self.available_models = available_models
        self.current_model = current_model
        self.window = window or (sublime.active_window() if sublime else None)

    def name(self):
        return "model"

    def next_input(self, args):
        model = args.get("model")
        if not model or model == "default":
            return None

        window = self.window or (sublime.active_window() if sublime else None)
        agent = get_current_agent(window)
        supports_effort = False
        supported_efforts = None
        if agent == AGENT_ANTIGRAVITY:
            if self.available_models:
                for m in self.available_models:
                    if m.get("modelId") == model or m.get("value") == model:
                        supports_effort = m.get("supportsEffort", False)
                        supported_efforts = m.get("supportedReasoningEfforts")
                        break
            else:
                supports_effort = True
        elif self.available_models:
            for m in self.available_models:
                if (m.get("modelId") == model or m.get("value") == model) and (m.get("supportsEffort") or m.get("supportedReasoningEfforts")):
                    supports_effort = True
                    supported_efforts = m.get("supportedReasoningEfforts")
                    break

        if supports_effort:
            current_effort = get_current_effort(window, agent=agent)
            return GeminiSetEffortListHandler(current_effort=current_effort, selected_model=model, supported_efforts=supported_efforts, window=window)
        return None

    def list_items(self):
        items = []
        for m in self.available_models:
            name = m.get("name", m.get("modelId", ""))
            desc = m.get("description", "")
            if desc and not name.endswith(f"({desc})"):
                name = f"{name}: ({desc})"
            items.append((name, m.get("modelId", "")))

        if self.current_model:
            window = self.window or (sublime.active_window() if sublime else None)
            agent = get_current_agent(window)
            current_effort = get_current_effort(window, agent=agent)

            supports_effort = False
            for m in self.available_models:
                m_id = m.get("modelId") or m.get("value")
                if m_id == self.current_model and (
                    m.get("supportsEffort") or m.get("supportedReasoningEfforts")
                ):
                    supports_effort = True
                    break
            if not self.available_models and agent == AGENT_ANTIGRAVITY:
                supports_effort = True

            for i, item in enumerate(items):
                is_match = (
                    item[1] == self.current_model
                    or (self.current_model in ("default", "") and item[1] == "")
                )
                if is_match:
                    name, val = items.pop(i)
                    if supports_effort and current_effort and val and val != "default":
                        name = f"{name}\t{current_effort}"
                    items.insert(0, (name, val))
                    break

        return items

    def placeholder(self):
        if self.current_model and self.current_model != "default":
            return f" ( {self.current_model} ); select a model"
        return "select a model"


class GeminiSetModelTextHandler(sublime_plugin.TextInputHandler):
    def __init__(self, current_model, window=None):
        self.current_model = current_model
        self.window = window or (sublime.active_window() if sublime else None)

    def name(self):
        return "model"

    def next_input(self, args):
        model = args.get("model")
        if not model or model == "default":
            return None
        window = self.window or (sublime.active_window() if sublime else None)
        agent = get_current_agent(window)
        if agent == AGENT_ANTIGRAVITY:
            current_effort = get_current_effort(window, agent=agent)
            return GeminiSetEffortListHandler(current_effort=current_effort, selected_model=model, window=window)
        return None

    def placeholder(self):
        if self.current_model and self.current_model != "default":
            return f"Enter model ID (current: {self.current_model})"
        return "Enter model ID (e.g., gemini-3.8-flash, or 'default')"

    def description(self, text):
        return "Set Model: " + text if text else "Set Model"

    def validate(self, text):
        return len(text.strip()) > 0


class GeminiSetModelCommand(sublime_plugin.WindowCommand):
    """Set the model and optional reasoning effort for the current session."""
    def run(self, model=None, effort=None):
        agent = get_current_agent(self.window)
        agent_title = "Antigravity" if agent == AGENT_ANTIGRAVITY else "Gemini"
        agent_model_key = f"gemini_model_{agent}"
        agent_effort_key = f"gemini_effort_{agent}"

        if not model or model.strip().lower() in ("default", ""):
            self.window.settings().erase(agent_model_key)
            if agent == AGENT_GEMINI:
                self.window.settings().erase(GEMINI_MODEL)
            target_model = None
            status_msg = f"{agent_title} model set to: default (agent decided)"
        else:
            model = model.strip()
            self.window.settings().set(agent_model_key, model)
            self.window.settings().set(GEMINI_MODEL, model)
            target_model = model
            status_msg = f"{agent_title} model set to: {model}"

        target_effort = None
        if effort is not None:
            effort = effort.strip().lower()
            if effort in ("", "default"):
                self.window.settings().erase(agent_effort_key)
                self.window.settings().erase(GEMINI_EFFORT)
                target_effort = None
                if target_model:
                    status_msg += " (effort: default)"
            else:
                self.window.settings().set(agent_effort_key, effort)
                self.window.settings().set(GEMINI_EFFORT, effort)
                target_effort = effort
                status_msg += f" (effort: {effort})"
        else:
            target_effort = get_current_effort(self.window, agent=agent)

        sublime.status_message(status_msg)
        LOG.info("%s", status_msg)

        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            client = getattr(session, "client", None)
            if client:
                if getattr(client, "available_models", None) and target_model:
                    for m in client.available_models:
                        if (m.get("modelId") or m.get("value")) == target_model:
                            if (
                                not m.get("supportsEffort")
                                and not m.get("supportedReasoningEfforts")
                            ):
                                target_effort = None
                            break
                client.current_model_id = target_model
                if hasattr(client, "current_effort"):
                    client.current_effort = target_effort
                if (
                    getattr(client, "session_id", None)
                    or agent == AGENT_ANTIGRAVITY
                ):
                    client.agent_session_set_model(
                        target_model, effort=target_effort
                    )

            chat_view = getattr(session, "chat_view", None)
            if (
                chat_view
                and chat_view.is_valid()
                and (
                    not getattr(session, "is_startup", False)
                    or chat_view.settings().has(GEMINI_INPUT_START)
                )
            ):
                tag = format_model_tag(target_model, target_effort)
                chat_view.run_command("gemini_chat_append", {"text": f"{tag}\n\n"})

    def input(self, args):
        if "model" not in args:
            window_id = self.window.id()
            agent = get_current_agent(self.window)
            current_model = get_current_model(self.window, agent=agent) or "default"

            if window_id in gemini_clients:
                session = gemini_clients[window_id]
                client = getattr(session, "client", None)
                if client and getattr(client, "available_models", None):
                    if current_model == "default" and getattr(client, "current_model_id", None):
                        current_model = client.current_model_id
                    return GeminiSetModelListHandler(client.available_models, current_model, window=self.window)

            return GeminiSetModelTextHandler(current_model, window=self.window)
        return None


class GeminiSetEffortCommand(sublime_plugin.WindowCommand):
    """Set the reasoning effort for the current agent session."""
    def run(self, effort=None):
        if effort is not None:
            effort = effort.strip().lower()

        agent = get_current_agent(self.window)
        agent_effort_key = f"gemini_effort_{agent}"

        if not effort or effort == "default":
            self.window.settings().erase(agent_effort_key)
            self.window.settings().erase(GEMINI_EFFORT)
            target_effort = None
            effort_label = "default (agent decided)"
        else:
            self.window.settings().set(agent_effort_key, effort)
            self.window.settings().set(GEMINI_EFFORT, effort)
            target_effort = effort
            effort_label = effort

        agent_title = "Antigravity" if agent == AGENT_ANTIGRAVITY else "Gemini"
        sublime.status_message(f"{agent_title} reasoning effort set to: {effort_label}")
        LOG.info("%s reasoning effort set to: %s", agent_title, effort_label)

        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            client = getattr(session, "client", None)
            if client:
                if hasattr(client, "agent_session_set_effort"):
                    client.agent_session_set_effort(target_effort)
                elif hasattr(client, "current_effort"):
                    client.current_effort = target_effort

            current_model = None
            if client:
                current_model = getattr(client, "current_model_id", None)
            if not current_model:
                current_model = get_current_model(self.window, agent=agent)

            chat_view = getattr(session, "chat_view", None)
            if (
                chat_view
                and chat_view.is_valid()
                and (
                    not getattr(session, "is_startup", False)
                    or chat_view.settings().has(GEMINI_INPUT_START)
                )
            ):
                tag = format_model_tag(current_model, target_effort)
                chat_view.run_command("gemini_chat_append", {"text": f"{tag}\n\n"})

    def input(self, args):
        if "effort" not in args:
            agent = get_current_agent(self.window)
            current_effort = get_current_effort(self.window, agent=agent)
            return GeminiSetEffortListHandler(current_effort=current_effort, window=self.window)
        return None

    def is_enabled(self):
        agent = get_current_agent(self.window)
        if agent == AGENT_ANTIGRAVITY:
            return True
        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            client = getattr(session, "client", None)
            if client and (hasattr(client, "agent_session_set_effort") or hasattr(client, "current_effort")):
                return True
        return False


class GeminiClearSessionCommand(sublime_plugin.WindowCommand):
    """
    Clears the current chat session by disconnecting and reconnecting the agent.
    This resets the conversation history.
    """
    def run(self):
        window_id = self.window.id()
        if window_id not in gemini_clients:
            sublime.status_message("No active Gemini session found")
            return

        # Reset the session (disconnect and reconnect agent)
        session = gemini_clients[window_id]
        session.clear_session()
        sublime.status_message("Resetting Gemini session...")
        LOG.info("Resetting Gemini session via disconnect/reconnect")

    def is_enabled(self):
        # Only enable if there's an active session
        return self.window.id() in gemini_clients


class GeminiSwitchAgentListHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_agent):
        self.current_agent = current_agent

    def name(self):
        return "agent"

    def list_items(self):
        items = [
            ("Antigravity (antigravity)", AGENT_ANTIGRAVITY),
            ("Gemini CLI (gemini)", AGENT_GEMINI),
        ]
        if self.current_agent:
            for i, item in enumerate(items):
                if item[1] == self.current_agent:
                    items.insert(0, items.pop(i))
                    break
        return items

    def placeholder(self):
        if self.current_agent:
            return f" ( current: {self.current_agent} ); select agent provider"
        return "select agent provider"


class GeminiSwitchAgentCommand(sublime_plugin.WindowCommand):
    """Switch between Gemini CLI and Antigravity agents."""
    def run(self, agent):
        if not agent:
            return
        agent = agent.strip().lower()
        if agent not in (AGENT_GEMINI, AGENT_ANTIGRAVITY):
            sublime.error_message(f"Unknown agent: '{agent}'. Valid options are '{AGENT_GEMINI}' or '{AGENT_ANTIGRAVITY}'.")
            return

        self.window.settings().set(GEMINI_AGENT, agent)
        sublime.status_message(f"Agent switched to: {agent}")
        LOG.info("Agent provider switched to: %s", agent)

        window_id = self.window.id()
        if window_id in gemini_clients:
            session = gemini_clients[window_id]
            session.switch_agent(agent)

    def input(self, args):
        if "agent" not in args:
            current_agent = get_current_agent(self.window)
            return GeminiSwitchAgentListHandler(current_agent)
        return None


class GeminiResumeSessionCommand(sublime_plugin.WindowCommand):
    """
    Shows a quick panel listing past Antigravity sessions for the current workspace.
    Resumes conversation in an active or newly opened chat view.
    Only supported for Antigravity (agy).
    """
    _PREVIEW_LEN = 140

    def _get_cwd(self, session):
        if session is not None:
            return session.cwd or get_best_dir(session.chat_view)
        custom_cwd = self.window.settings().get(GEMINI_ACTIVE_WORKSPACE)
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd
        folders = self.window.folders()
        return folders[0] if folders else ""

    def run(self):
        import datetime
        window_id = self.window.id()
        session = gemini_clients.get(window_id)
        cwd = self._get_cwd(session)

        # 1. Fetch sessions for cwd
        sessions = list_antigravity_sessions(cwd)
        if not sessions and cwd:
            sessions = list_antigravity_sessions(None)

        if not sessions:
            sublime.status_message("No past Antigravity sessions found")
            return

        current_session_id = None
        if session and session.client:
            current_session_id = getattr(session.client, "session_id", None) or session.chat_view.settings().get(GEMINI_SESSION_ID)

        items = []
        for s in sessions:
            sid = s["session_id"]
            summary = s["summary"] or "(empty)"
            if len(summary) > self._PREVIEW_LEN:
                summary = summary[:self._PREVIEW_LEN] + "…"
            dt = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M") if s.get("mtime") else ""
            base_title = f"{dt}  [{sid[:8]}]".strip() if dt else f"[{sid[:8]}]"
            title = f"{base_title}\t⦿" if sid == current_session_id else base_title
            items.append([title, summary])

        def on_select(index):
            if index < 0:
                return
            chosen = sessions[index]
            chosen_id = chosen["session_id"]
            if chosen_id == current_session_id:
                sublime.status_message("Already on that session")
                return

            self.window.settings().set(GEMINI_AGENT, AGENT_ANTIGRAVITY)
            active_session = gemini_clients.get(window_id)
            if active_session is not None:
                active_session.resume_session(chosen_id)
            else:
                self.window.run_command("gemini_cli", {"session_id": chosen_id})

            sublime.status_message(f"Resuming Antigravity session {chosen_id[:8]}…")

        self.window.show_quick_panel(items, on_select, placeholder="Resume previous Antigravity session")

