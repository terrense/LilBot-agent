from __future__ import annotations

import unittest

from lilbot.tui.dashboard import _highlight_trace_line


class DashboardTraceTests(unittest.TestCase):
    def test_trace_highlights_markdown_and_tool_cards(self):
        heading = _highlight_trace_line("### Harness Engineering")
        self.assertEqual(heading[0][0], "class:trace.heading")

        inline = _highlight_trace_line("Use `read_file` and **review** output.")
        styles = [style for style, _text in inline]
        self.assertIn("class:trace.code.inline", styles)
        self.assertIn("class:trace.bold", styles)

        tool = _highlight_trace_line("╭─ ▷ run 120000-01  read_file")
        self.assertEqual(tool[0][0], "class:trace.tool.rail")
        self.assertIn("class:trace.tool", [style for style, _text in tool])


if __name__ == "__main__":
    unittest.main()
