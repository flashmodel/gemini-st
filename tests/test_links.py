import os
import sys
import unittest
from unittest.mock import MagicMock

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock sublime before importing chat.links
mock_sublime = MagicMock()
mock_sublime.ENCODED_POSITION = 1
mock_sublime.Region = lambda a, b: (a, b)
sys.modules["sublime"] = mock_sublime

from chat.links import (
    _is_windows_abs_path,
    _resolve_path,
    _parse_markdown_file_target,
    open_local_file_link,
    parse_tool_file_line,
    parse_diff_hunk_line,
    open_tool_file_link,
)


class TestMarkdownLinks(unittest.TestCase):

    def setUp(self):
        self.cwd = BASE_DIR
        self.existing_file = os.path.join(self.cwd, "gemini_cli.py")
        self.assertTrue(os.path.isfile(self.existing_file))

    def test_is_windows_abs_path(self):
        self.assertTrue(_is_windows_abs_path("C:\\Users\\test"))
        self.assertTrue(_is_windows_abs_path("D:/projects/repo"))
        self.assertTrue(_is_windows_abs_path("\\\\server\\share\\file"))
        self.assertTrue(_is_windows_abs_path("//server/share/file"))
        self.assertFalse(_is_windows_abs_path("/Users/test"))
        self.assertFalse(_is_windows_abs_path("relative/path"))

    def test_resolve_path(self):
        self.assertEqual(_resolve_path(self.existing_file, "/tmp"), self.existing_file)
        rel = _resolve_path("gemini_cli.py", self.cwd)
        self.assertEqual(rel, self.existing_file)

    def test_parse_file_uri_with_line_range(self):
        target = f"file://{self.existing_file}#L2281-L2320"
        result = _parse_markdown_file_target(target, self.cwd)
        self.assertIsNotNone(result)
        path, line, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line, 2281)
        self.assertIsNone(col)

    def test_parse_file_uri_with_single_line(self):
        target = f"file://{self.existing_file}#L100"
        result = _parse_markdown_file_target(target, self.cwd)
        self.assertIsNotNone(result)
        path, line, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line, 100)
        self.assertIsNone(col)

    def test_parse_file_uri_with_line_and_column(self):
        target = f"file://{self.existing_file}#L100C5"
        result = _parse_markdown_file_target(target, self.cwd)
        self.assertIsNotNone(result)
        path, line, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line, 100)
        self.assertEqual(col, 5)

    def test_parse_colon_line_format(self):
        target = f"{self.existing_file}:50:10"
        result = _parse_markdown_file_target(target, self.cwd)
        self.assertIsNotNone(result)
        path, line, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line, 50)
        self.assertEqual(col, 10)

    def test_parse_relative_path(self):
        target = "gemini_cli.py#L42"
        result = _parse_markdown_file_target(target, self.cwd)
        self.assertIsNotNone(result)
        path, line, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line, 42)

    def test_parse_ignores_web_urls(self):
        self.assertIsNone(_parse_markdown_file_target("https://github.com/test/repo", self.cwd))
        self.assertIsNone(_parse_markdown_file_target("http://example.com/file.py", self.cwd))
        self.assertIsNone(_parse_markdown_file_target("mailto:user@example.com", self.cwd))

    def test_parse_non_existent_file(self):
        target = f"file://{self.cwd}/non_existent_file_xyz123.py#L10"
        self.assertIsNone(_parse_markdown_file_target(target, self.cwd))

    def test_open_local_file_link_double_click_on_label(self):
        line = f"- In [`GeminiSetModelCommand.run`](file://{self.existing_file}#L2281-L2320)"
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window

        line_start = 500
        view.line.return_value = MagicMock(begin=lambda: line_start, end=lambda: line_start + len(line))
        view.substr.return_value = line

        # Click on `GeminiSetModelCommand` (offset ~10 in line)
        click_point = line_start + 10
        handled = open_local_file_link(view, click_point, window=window, cwd=self.cwd)
        self.assertTrue(handled)
        expected_encoded = f"{self.existing_file}:2281:0"
        window.open_file.assert_called_with(expected_encoded, mock_sublime.ENCODED_POSITION)

    def test_open_local_file_link_double_click_on_url(self):
        line = f"- In [`GeminiSetModelCommand.run`](file://{self.existing_file}#L2281-L2320)"
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window

        line_start = 500
        view.line.return_value = MagicMock(begin=lambda: line_start, end=lambda: line_start + len(line))
        view.substr.return_value = line

        # Click inside the URL part
        url_idx = line.index("file://") + 5
        click_point = line_start + url_idx
        handled = open_local_file_link(view, click_point, window=window, cwd=self.cwd)
        self.assertTrue(handled)
        expected_encoded = f"{self.existing_file}:2281:0"
        window.open_file.assert_called_with(expected_encoded, mock_sublime.ENCODED_POSITION)

    def test_open_local_file_link_double_click_outside_link(self):
        line = f"- In [`GeminiSetModelCommand.run`](file://{self.existing_file}#L2281-L2320)"
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window

        line_start = 500
        view.line.return_value = MagicMock(begin=lambda: line_start, end=lambda: line_start + len(line))
        view.substr.return_value = line

        # Click on `- In ` (offset 2)
        click_point = line_start + 2
        handled = open_local_file_link(view, click_point, window=window, cwd=self.cwd)
        self.assertFalse(handled)
        window.open_file.assert_not_called()

    def test_open_bare_file_url(self):
        line = f"Check file://{self.existing_file}#L15 for details"
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window

        line_start = 100
        view.line.return_value = MagicMock(begin=lambda: line_start, end=lambda: line_start + len(line))
        view.substr.return_value = line

        click_point = line_start + line.index("gemini_cli.py")
        handled = open_local_file_link(view, click_point, window=window, cwd=self.cwd)
        self.assertTrue(handled)
        expected_encoded = f"{self.existing_file}:15:0"
        window.open_file.assert_called_with(expected_encoded, mock_sublime.ENCODED_POSITION)


class TestToolFileLinks(unittest.TestCase):

    def setUp(self):
        self.cwd = BASE_DIR
        self.existing_file = os.path.join(self.cwd, "gemini_cli.py")
        self.assertTrue(os.path.isfile(self.existing_file))

    def test_parse_tool_file_line_absolute(self):
        line = f"⏺ Read {self.existing_file}"
        result = parse_tool_file_line(line, self.cwd)
        self.assertIsNotNone(result)
        path, line_no, col = result
        self.assertEqual(path, self.existing_file)
        self.assertIsNone(line_no)
        self.assertIsNone(col)

    def test_parse_tool_file_line_relative(self):
        line = "⏺ Edit gemini_cli.py"
        result = parse_tool_file_line(line, self.cwd)
        self.assertIsNotNone(result)
        path, line_no, col = result
        self.assertEqual(path, self.existing_file)
        self.assertIsNone(line_no)

    def test_parse_tool_file_line_with_line_hash(self):
        line = "⏺ Read gemini_cli.py#L42"
        result = parse_tool_file_line(line, self.cwd)
        self.assertIsNotNone(result)
        path, line_no, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line_no, 42)
        self.assertIsNone(col)

    def test_parse_tool_file_line_with_colon_pos(self):
        line = "⏺ Read gemini_cli.py:55:10"
        result = parse_tool_file_line(line, self.cwd)
        self.assertIsNotNone(result)
        path, line_no, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line_no, 55)
        self.assertEqual(col, 10)

    def test_parse_tool_file_line_quoted(self):
        line = '⏺ Edit "gemini_cli.py"'
        result = parse_tool_file_line(line, self.cwd)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], self.existing_file)

    def test_parse_tool_file_line_non_existent(self):
        line = "⏺ Read does_not_exist_file_xyz.py"
        self.assertIsNone(parse_tool_file_line(line, self.cwd))

    def test_parse_diff_hunk_line(self):
        view = MagicMock()
        # Mock view with two lines:
        # line 0 (pt 0): ⏺ Edit gemini_cli.py
        # line 1 (pt 30): @@ -10,5 +25,8 @@
        tool_line = "⏺ Edit gemini_cli.py"
        hunk_line = "@@ -10,5 +25,8 @@"

        def mock_line(pt):
            if pt >= 30:
                return MagicMock(begin=lambda: 30, end=lambda: 30 + len(hunk_line))
            return MagicMock(begin=lambda: 0, end=lambda: len(tool_line))

        def mock_substr(reg):
            if reg.begin() >= 30:
                return hunk_line
            return tool_line

        view.line.side_effect = mock_line
        view.substr.side_effect = mock_substr

        result = parse_diff_hunk_line(view, point=35, cwd=self.cwd)
        self.assertIsNotNone(result)
        path, line_no, col = result
        self.assertEqual(path, self.existing_file)
        self.assertEqual(line_no, 25)

    def test_open_tool_file_link(self):
        line = "⏺ Read gemini_cli.py#L100"
        view = MagicMock()
        window = MagicMock()
        view.window.return_value = window

        view.line.return_value = MagicMock(begin=lambda: 0, end=lambda: len(line))
        view.substr.return_value = line

        handled = open_tool_file_link(view, point=5, cwd=self.cwd, window=window)
        self.assertTrue(handled)
        window.open_file.assert_called_with(f"{self.existing_file}:100:0", mock_sublime.ENCODED_POSITION)


if __name__ == "__main__":
    unittest.main()
