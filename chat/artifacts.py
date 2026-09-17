"""
Dedicated Antigravity artifacts module for GeminiCLI.

Handles detection, tracking, and rendering of Antigravity agent artifacts
(plans such as implementation_plan.md / *_plan.md and walkthroughs such as walkthrough.md)
in Sublime Text.
"""

import os
import json
import logging
import re
try:
    import sublime
except ImportError:
    sublime = None

LOG = logging.getLogger(__package__ or "GeminiCLI")

ARTIFACT_REGION_KEY = "gemini_artifact_regions"
ARTIFACT_REGION_SCOPE = "region.bluish"
ARTIFACT_PHANTOM_KEY = "gemini_artifacts"

class ArtifactItem:
    """Represents a discovered plan or walkthrough artifact."""

    def __init__(self, path, name=None, kind="artifact", label="Artifact", title="", mtime=0.0, content="", can_execute=False):
        self.path = path
        self.name = name or (os.path.basename(path) if path else "artifact.md")
        self.kind = kind  # "plan", "walkthrough", "artifact"
        self.label = label  # "Plan", "Walkthrough", "Artifact"
        self.title = title
        self.mtime = mtime
        self.content = content
        self.can_execute = can_execute

    def to_dict(self):
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "label": self.label,
            "title": self.title,
            "mtime": self.mtime,
            "content": self.content,
            "can_execute": self.can_execute,
        }

    def __repr__(self):
        return f"<ArtifactItem kind={self.kind} name={self.name} path={self.path}>"


def extract_title_from_markdown(content):
    """Extract a short title/summary from markdown content (first # Heading)."""
    if not content:
        return ""
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("#"):
            clean = s.lstrip("#").strip()
            if clean.startswith("[") and clean.endswith("]"):
                clean = clean[1:-1].strip()
            if clean:
                return clean
    return ""


def read_artifact_metadata(file_path):
    """Read companion <file_path>.metadata.json if it exists."""
    if not file_path:
        return {}
    meta_path = file_path + ".metadata.json"
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
                return json.load(f)
        except Exception as e:
            LOG.debug("Failed to read metadata %s: %s", meta_path, e)
    return {}


def get_brain_dirs(session_id, extra_env=None):
    """Return existing Antigravity brain directories for session_id."""
    if not session_id:
        return []

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    candidates = []
    cli_dir = env.get("GEMINI_CLI_DIR")
    if cli_dir:
        candidates.append(os.path.join(os.path.expanduser(cli_dir), "brain", session_id))

    home = os.path.expanduser("~")
    candidates.extend([
        os.path.join(home, ".gemini", "antigravity-cli", "brain", session_id),
        os.path.join(home, ".gemini", "antigravity-ide", "brain", session_id),
        os.path.join(home, ".gemini", "antigravity", "brain", session_id),
        os.path.join(home, ".gemini", "brain", session_id),
    ])

    existing = []
    for d in candidates:
        try:
            real = os.path.realpath(d)
        except (OSError, ValueError):
            continue
        if os.path.isdir(real) and real not in existing:
            existing.append(real)
    return existing


def is_plan_file(path_or_name):
    """Check if the given path or name corresponds to a plan artifact."""
    if not path_or_name:
        return False
    base = os.path.basename(path_or_name).lower()
    return "plan" in base and base.endswith(".md")


def is_walkthrough_file(path_or_name):
    """Check if the given path or name corresponds to a walkthrough artifact."""
    if not path_or_name:
        return False
    base = os.path.basename(path_or_name).lower()
    return "walkthrough" in base and base.endswith(".md")


def classify_artifact(file_path, metadata=None):
    """Classify artifact into (kind, label, priority)."""
    base = os.path.basename(file_path).lower() if file_path else ""
    meta = metadata or {}
    if is_plan_file(base) or meta.get("requestFeedback") is True:
        return "plan", "Plan", 0
    if is_walkthrough_file(base):
        return "walkthrough", "Walkthrough", 1
    return "artifact", "Artifact", 2


def is_user_artifact_tool_call(tool_name, params):
    """Check if a tool call creates or modifies an artifact."""
    if tool_name not in ("write_to_file", "replace_file_content", "edit_file", "create_file"):
        return False
    target = params.get("TargetFile") or params.get("file_path") or params.get("path") or ""
    meta = params.get("ArtifactMetadata") or {}
    if meta.get("RequestFeedback") is True or meta.get("UserFacing") is True:
        return True
    if is_plan_file(target) or is_walkthrough_file(target):
        return True
    return False


def open_artifact(window, abs_path, item=None):
    """Open or focus a view displaying the artifact markdown."""
    if not window:
        return None

    if abs_path and os.path.isfile(abs_path):
        return window.open_file(abs_path)

    # If already open as a scratch view
    for v in window.views():
        if (abs_path and v.file_name() == abs_path) or (abs_path and v.settings().get("gemini_artifact_path") == abs_path):
            window.focus_view(v)
            return v

    content = item.content if item else ""
    name = item.name if item else (os.path.basename(abs_path) if abs_path else "artifact.md")
    v = window.new_file()
    v.set_name(name)
    if abs_path:
        v.settings().set("gemini_artifact_path", abs_path)
    if content:
        v.run_command("append", {"characters": content})
    if hasattr(v, "assign_syntax"):
        v.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
    v.set_scratch(True)
    v.set_read_only(True)
    return v


class AntigravityArtifactManager:
    """
    Manages discovery, tracking, and rendering of Antigravity artifacts for a chat session.
    """

    def __init__(self, view, window, input_start_fn=None):
        self.view = view
        self.window = window
        self.input_start_fn = input_start_fn
        self.artifacts = {}  # abs_path -> ArtifactItem
        self.synced_mtimes = {}  # abs_path -> float
        self.pending_render = []  # list of abs_paths
        self.rendered_regions = []  # list of (Region, abs_path, ArtifactItem)
        self.phantom_set = sublime.PhantomSet(view, ARTIFACT_PHANTOM_KEY) if (sublime and hasattr(sublime, "PhantomSet")) else None

    def record_from_tool_call(self, tool_call):
        """Record an artifact if generated or modified by a tool call."""
        tool_name = tool_call.get("name") or tool_call.get("title") or ""
        params = tool_call.get("parameters") or {}
        if not is_user_artifact_tool_call(tool_name, params):
            return

        target = params.get("TargetFile") or params.get("file_path") or params.get("path") or ""
        if not target:
            return

        meta = params.get("ArtifactMetadata") or {}
        content = params.get("CodeContent") or params.get("ReplacementContent") or ""
        kind, label, _ = classify_artifact(target, meta)

        title = meta.get("Summary") or extract_title_from_markdown(content)
        mtime = 0.0
        if os.path.isfile(target):
            try:
                mtime = os.path.getmtime(target)
            except OSError:
                mtime = 0.0

        item = ArtifactItem(
            path=target,
            name=os.path.basename(target),
            kind=kind,
            label=label,
            title=title,
            mtime=mtime,
            content=content,
            can_execute=(kind == "plan")
        )
        self.artifacts[target] = item
        if target not in self.pending_render:
            self.pending_render.append(target)
        LOG.debug("Artifact recorded from tool call: %s", target)

    def scan_session_artifacts(self, session_id, extra_env=None):
        """Scan Antigravity brain directories for new/modified plan & walkthrough files."""
        if not session_id:
            return []

        discovered = []
        brain_dirs = get_brain_dirs(session_id, extra_env)

        for bdir in brain_dirs:
            try:
                files = os.listdir(bdir)
            except OSError:
                continue

            for fname in files:
                if fname.startswith(".") or fname.endswith(".metadata.json"):
                    continue
                if not fname.endswith(".md"):
                    continue

                fpath = os.path.join(bdir, fname)
                if not os.path.isfile(fpath):
                    continue

                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    mtime = 0.0

                last_mtime = self.synced_mtimes.get(fpath, 0.0)
                if last_mtime > 0 and mtime <= last_mtime and fpath not in self.pending_render:
                    continue

                meta = read_artifact_metadata(fpath)
                title = meta.get("summary") or ""
                if not title:
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            sample = f.read(4096)
                            title = extract_title_from_markdown(sample)
                    except Exception:
                        title = ""

                kind, label, _ = classify_artifact(fpath, meta)
                item = ArtifactItem(
                    path=fpath,
                    name=fname,
                    kind=kind,
                    label=label,
                    title=title,
                    mtime=mtime,
                    can_execute=(kind == "plan")
                )
                self.artifacts[fpath] = item
                self.synced_mtimes[fpath] = mtime
                if fpath not in self.pending_render:
                    self.pending_render.append(fpath)
                discovered.append(item)

        # Also refresh mtimes for any previously recorded paths
        for path, it in list(self.artifacts.items()):
            if os.path.isfile(path) and path not in self.pending_render:
                try:
                    mtime = os.path.getmtime(path)
                    if mtime > self.synced_mtimes.get(path, 0.0):
                        it.mtime = mtime
                        self.synced_mtimes[path] = mtime
                        self.pending_render.append(path)
                except OSError:
                    pass

        return discovered

    def sync_and_render(self, session_id, extra_env=None):
        """Dual-trigger sync and display for Antigravity artifacts."""
        self.scan_session_artifacts(session_id, extra_env)
        self.render_pending_artifacts()

    def render_pending_artifacts(self):
        """Append newly detected plan/walkthrough artifacts to the chat view."""
        if not self.pending_render:
            return

        items = []
        for p in self.pending_render:
            it = self.artifacts.get(p)
            if it:
                items.append(it)
        self.pending_render = []

        if not items:
            return

        def _sort_key(it):
            if it.kind == "plan":
                return (0, it.name)
            if it.kind == "walkthrough":
                return (1, it.name)
            return (2, it.name)

        items.sort(key=_sort_key)

        if not self.view:
            return

        start_pos = self.input_start_fn(self.view) if self.input_start_fn else None
        base_pos = (start_pos - 1) if start_pos is not None else (self.view.size() if hasattr(self.view, "size") else 0)
        text = "\n"
        curr_offset = len(text)
        region_specs = []

        for it in items:
            line = f"▣ {it.name}\n"
            start = base_pos + curr_offset
            end = start + len(line.rstrip("\n"))
            region_specs.append((start, end, it.path, it))
            text += line
            curr_offset += len(line)

        # Append to view
        self.view.run_command("gemini_chat_append", {"text": text})

        # Track rendered regions
        if sublime:
            for start, end, path, it in region_specs:
                reg = sublime.Region(start, end)
                self.rendered_regions.append((reg, path, it))
            self._redraw_regions()
            self._update_phantoms()

    def _redraw_regions(self):
        """Redraw underline/highlight regions for artifact lines."""
        if not self.view or not sublime:
            return
        regs = [r for r, _, _ in self.rendered_regions if not (hasattr(r, "empty") and r.empty())]
        if regs:
            flags = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
            self.view.add_regions(
                ARTIFACT_REGION_KEY,
                regs,
                ARTIFACT_REGION_SCOPE,
                "",
                flags
            )
        else:
            self.view.erase_regions(ARTIFACT_REGION_KEY)

    def _update_phantoms(self):
        """Update inline phantoms with clickable styled button controls."""
        if not self.phantom_set or not sublime:
            return
        phantoms = []
        for region, path, item in self.rendered_regions:
            pt = region.end()
            phantom_reg = sublime.Region(pt, pt)
            html = f"""
            <body id="gemini-artifact-inline" style="margin:0;padding:0;">
                <style>
                    a.open-btn {{
                        display: inline;
                        background-color: var(--bluish);
                        color: var(--background);
                        border: 1px solid var(--bluish);
                        border-radius: 3px;
                        padding: 2px 8px;
                        font-size: 12px;
                        font-weight: bold;
                        margin-left: 8px;
                        text-decoration: none;
                    }}
                </style>
                <a href="open:{path}" class="open-btn">Open</a>
            </body>
            """
            phantoms.append(sublime.Phantom(
                phantom_reg,
                html,
                sublime.LAYOUT_INLINE,
                on_navigate=self.handle_phantom_navigate
            ))
        self.phantom_set.update(phantoms)

    def handle_phantom_navigate(self, href):
        """Handle link clicks from artifact phantoms."""
        if href.startswith("open:"):
            path = href[5:]
            self.open_artifact(path)

    def open_artifact(self, path, item=None):
        """Open the artifact file in Sublime Text."""
        it = item or self.artifacts.get(path)
        return open_artifact(self.window, path, it)

    def open_artifact_at(self, point):
        """Open artifact if point matches a rendered region or line."""
        if point is None:
            return False

        for reg, path, it in self.rendered_regions:
            if hasattr(reg, "contains") and reg.contains(point):
                self.open_artifact(path, it)
                return True

        if self.view and hasattr(self.view, "substr") and hasattr(self.view, "line"):
            line_str = self.view.substr(self.view.line(point)).strip()
            if line_str.startswith("▣"):
                for path, it in self.artifacts.items():
                    if it.name in line_str:
                        self.open_artifact(path, it)
                        return True
        return False

    def get_all_artifacts(self):
        """Return list of all recorded artifacts."""
        return list(self.artifacts.values())

    def clear(self):
        """Reset all tracked artifacts and clear phantoms/regions."""
        self.artifacts.clear()
        self.synced_mtimes.clear()
        self.pending_render.clear()
        self.rendered_regions.clear()
        if self.view and sublime:
            self.view.erase_regions(ARTIFACT_REGION_KEY)
        if self.phantom_set:
            self.phantom_set.update([])

