"""
Dedicated links navigation module for GeminiCLI.

Handles resolving and opening Markdown file links (e.g. [name](file:///path#L123-L145))
and bare file:// URIs in Sublime Text upon double-click events in the chat view.
"""

import logging
import os
import re
import urllib.parse

try:
    import sublime
    import sublime_plugin
except ImportError:
    sublime = None
    sublime_plugin = None

LOG = logging.getLogger(__package__ or "GeminiCLI")

_WINDOWS_DRIVE_PATH_RE = re.compile(r'^[a-zA-Z]:[\\/]')
_MARKDOWN_LINK_RE = re.compile(
    r'(?<!!)\[([^\n\]]*)\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:\s+["\'][^"\']*["\'])?\s*\)?'
)
_BARE_FILE_LINK_RE = re.compile(r'file://[^\s()<>"]+')

_TOOL_FILE_NAMES = (
    "Read", "Edit", "Write",
    "read", "edit", "write",
)
_TOOL_NAMES_ALT = "|".join(re.escape(n) for n in _TOOL_FILE_NAMES)

_TOOL_FILE_LINE_RE = re.compile(
    rf'^⏺\s*(?:{_TOOL_NAMES_ALT})\s+(.+?)(?:#L(\d+)(?:-L(\d+)|C(\d+))?|:(\d+)(?::(\d+))?)?(?:,.*)?$'
)
_HUNK_LINE_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def _is_windows_abs_path(path):
    """Check if path is a Windows absolute path."""
    return bool(_WINDOWS_DRIVE_PATH_RE.match(path)) or path.startswith(
        ("\\\\", "//")
    )


def _resolve_path(path_part, cwd):
    """Resolve a path against cwd if relative."""
    if os.path.isabs(path_part) or _is_windows_abs_path(path_part):
        return path_part
    return os.path.normpath(os.path.join(cwd, path_part)) if cwd else path_part


def _parse_markdown_file_target(target, cwd):
    """Resolve a Markdown link target or file URI to (absolute path, line, column).

    Supports:
        file:///path/to/file#L123-L145
        file:///path/to/file#L123C5
        file:///path/to/file:123:5
        relative/path/to/file#L123
        relative/path/to/file:123
    """
    if not target:
        return None
    target = urllib.parse.unquote(target.strip())
    if target.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(target)
        path = parsed.path
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        elif re.match(r'^/[a-zA-Z]:[\\/]', path):
            path = path[1:]
        target = path
        if parsed.fragment:
            target += f"#{parsed.fragment}"
    elif (
        not _is_windows_abs_path(target)
        and re.match(r'^[a-z][a-z0-9+.-]*:', target, re.IGNORECASE)
    ):
        return None

    line = None
    column = None
    fragment = re.search(
        r'#L(\d+)(?:C(\d+))?(?:-L?(\d+)(?:C(\d+))?)?$', target
    )
    if fragment:
        line = int(fragment.group(1))
        column = int(fragment.group(2)) if fragment.group(2) else None
        target = target[:fragment.start()]
    else:
        encoded_position = re.match(r'^(.*?):(\d+)(?::(\d+))?$', target)
        if encoded_position:
            target = encoded_position.group(1)
            line = int(encoded_position.group(2))
            column = (
                int(encoded_position.group(3))
                if encoded_position.group(3)
                else None
            )

    path = _resolve_path(target, cwd)
    if not os.path.isfile(path):
        return None
    return path, line, column


def open_local_file_link(view, point, window=None, cwd=None):
    """Open the local Markdown link or bare file URL at point.

    Returns True if a valid local file link was handled and opened.
    """
    if view is None or point is None:
        return False

    window = window or (view.window() if hasattr(view, "window") else None)
    if not window and sublime:
        window = sublime.active_window()
    if not window:
        return False

    if not cwd:
        try:
            import sys
            for mod_name in ("GeminiCLI.gemini_cli", "gemini_cli"):
                if mod_name in sys.modules:
                    mod = sys.modules[mod_name]
                    clients = getattr(mod, "gemini_clients", {})
                    if window.id() in clients:
                        cwd = getattr(clients[window.id()], "cwd", None)
                    if not cwd and hasattr(mod, "get_best_dir"):
                        cwd = mod.get_best_dir(view)
                    break
        except Exception:
            pass

    line_region = view.line(point)
    line_text = view.substr(line_region)
    column_in_line = point - line_region.begin()

    # 1. Match Markdown link: [label](target)
    for match in _MARKDOWN_LINK_RE.finditer(line_text):
        if not (match.start() <= column_in_line < match.end()):
            continue
        target = match.group(2) or match.group(3)
        result = _parse_markdown_file_target(target, cwd)
        if result is not None:
            abs_path, line, column = result
            if line is not None and sublime and hasattr(sublime, "ENCODED_POSITION"):
                encoded = f"{abs_path}:{line}:{column or 0}"
                window.open_file(encoded, sublime.ENCODED_POSITION)
            else:
                window.open_file(abs_path)
            return True

    # 2. Match bare file:// link: file:///path#L10
    for match in _BARE_FILE_LINK_RE.finditer(line_text):
        if not (match.start() <= column_in_line < match.end()):
            continue
        target = match.group(0)
        result = _parse_markdown_file_target(target, cwd)
        if result is not None:
            abs_path, line, column = result
            if line is not None and sublime and hasattr(sublime, "ENCODED_POSITION"):
                encoded = f"{abs_path}:{line}:{column or 0}"
                window.open_file(encoded, sublime.ENCODED_POSITION)
            else:
                window.open_file(abs_path)
            return True

    return False


def parse_tool_file_line(line_text, cwd=None):
    """Parse a tool line (e.g. ⏺ Read /path/to/file#L10) to (abs_path, line, column)."""
    if not line_text or not isinstance(line_text, str):
        return None
    m = _TOOL_FILE_LINE_RE.match(line_text.strip())
    if not m:
        return None
    raw_path = m.group(1).strip().strip('"`\'')
    line = None
    col = None
    if m.group(2):
        line = int(m.group(2))
        if m.group(4):
            col = int(m.group(4))
    elif m.group(5):
        line = int(m.group(5))
        if m.group(6):
            col = int(m.group(6))

    abs_path = _resolve_path(raw_path, cwd)
    if not os.path.isfile(abs_path):
        return None
    return abs_path, line, col


def parse_diff_hunk_line(view, point, cwd=None):
    """Resolve a diff hunk header (@@ -a,b +c,d @@) to (abs_path, line, column).

    Scans upward from the clicked point in the view to locate the nearest
    tool header line above the diff block.
    """
    if view is None or point is None:
        return None
    try:
        line_region = view.line(point)
        line_text = view.substr(line_region)
        if not line_text or not isinstance(line_text, str):
            return None
    except Exception:
        return None

    line_text_strip = line_text.strip()
    m = _HUNK_LINE_RE.match(line_text_strip)
    if not m:
        return None
    line_start = int(m.group(1))

    try:
        current_pt = line_region.begin() - 1
        while current_pt > 0:
            prev_line_region = view.line(current_pt)
            prev_line_text = view.substr(prev_line_region)
            if not isinstance(prev_line_text, str):
                break
            prev_line_text = prev_line_text.strip()
            tool_match = parse_tool_file_line(prev_line_text, cwd)
            if tool_match:
                abs_path, _, _ = tool_match
                return abs_path, line_start, 0
            if prev_line_text.startswith("⏺ "):
                break
            current_pt = prev_line_region.begin() - 1
    except Exception:
        return None
    return None


def open_tool_file_link(view, point, cwd=None, window=None):
    """Open the tool target file or diff hunk under point in Sublime Text.

    Returns True if a valid tool file link was handled and opened.
    """
    if view is None or point is None:
        return False

    window = window or (view.window() if hasattr(view, "window") else None)
    if not window and sublime:
        window = sublime.active_window()
    if not window:
        return False

    if not cwd:
        try:
            import sys
            for mod_name in ("GeminiCLI.gemini_cli", "gemini_cli"):
                if mod_name in sys.modules:
                    mod = sys.modules[mod_name]
                    clients = getattr(mod, "gemini_clients", {})
                    if window.id() in clients:
                        cwd = getattr(clients[window.id()], "cwd", None)
                    if not cwd and hasattr(mod, "get_best_dir"):
                        cwd = mod.get_best_dir(view)
                    break
        except Exception:
            pass

    try:
        line_region = view.line(point)
        line_text = view.substr(line_region)
    except Exception:
        return False

    result = parse_tool_file_line(line_text, cwd)
    if result is None:
        result = parse_diff_hunk_line(view, point, cwd)

    if result is None:
        return False

    abs_path, line, column = result
    if line is not None and sublime and hasattr(sublime, "ENCODED_POSITION"):
        encoded = f"{abs_path}:{line}:{column or 0}"
        window.open_file(encoded, sublime.ENCODED_POSITION)
    else:
        window.open_file(abs_path)
    return True
