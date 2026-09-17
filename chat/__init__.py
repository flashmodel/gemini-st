"""
Chat package for GeminiCLI.
Contains presentation, interactive components, and artifact management for the Gemini Chat view.
"""

from .artifacts import (
    ArtifactItem,
    AntigravityArtifactManager,
    extract_title_from_markdown,
    read_artifact_metadata,
    get_brain_dirs,
    is_plan_file,
    is_walkthrough_file,
    classify_artifact,
    is_user_artifact_tool_call,
    open_artifact,
)
from .autocomplete import (
    AutoComplete,
    AtQuery,
    parse_at_query_text,
    DEFAULT_IGNORED_NAMES,
)
from .links import (
    open_local_file_link,
    _parse_markdown_file_target,
    open_tool_file_link,
    parse_tool_file_line,
    parse_diff_hunk_line,
)

__all__ = [
    "ArtifactItem",
    "AntigravityArtifactManager",
    "extract_title_from_markdown",
    "read_artifact_metadata",
    "get_brain_dirs",
    "is_plan_file",
    "is_walkthrough_file",
    "classify_artifact",
    "is_user_artifact_tool_call",
    "open_artifact",
    "AutoComplete",
    "AtQuery",
    "parse_at_query_text",
    "DEFAULT_IGNORED_NAMES",
    "open_local_file_link",
    "_parse_markdown_file_target",
    "open_tool_file_link",
    "parse_tool_file_line",
    "parse_diff_hunk_line",
]
