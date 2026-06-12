from __future__ import annotations

import unittest

from lilbot.tui.dashboard import (
    LILBOT_LOGO_COMPACT_ROWS,
    LILBOT_LOGO_ROWS,
    _clip_line,
    _highlight_trace_line,
    _summarize_interim_text,
    _summarize_tool_output,
)


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

    def test_tool_output_summary_omits_noisy_git_paths(self):
        output = "\n".join(
            [".git/objects/ab/cdef", ".git/objects/cd/ef01", "__pycache__/x.pyc"]
            + [f"lilbot/file_{idx}.py" for idx in range(20)]
        )

        summary = _summarize_tool_output("list_dir", output)

        self.assertTrue(summary[0].startswith("output summarized:"))
        self.assertTrue(any("omitted 3 noisy" in line for line in summary))
        self.assertFalse(any(".git/objects" in line for line in summary))
        self.assertLessEqual(len(summary), 14)

    def test_interim_text_is_condensed(self):
        text = "\n".join(f"planning detail {idx}" for idx in range(12))

        summary = _summarize_interim_text(text)

        self.assertEqual(summary[0], "planning tool work; condensed intermediate reasoning.")
        self.assertTrue(summary[-1].startswith("... hidden"))

    def test_lilbot_logo_rows_are_fixed_banner_art(self):
        self.assertIn("██████", "\n".join(LILBOT_LOGO_ROWS))
        self.assertEqual(len(LILBOT_LOGO_COMPACT_ROWS), 3)
        self.assertLessEqual(len(_clip_line(LILBOT_LOGO_ROWS[0], 30)), 30)

    def test_line_start_offset_for_scroll_cursor(self):
        from lilbot.tui.dashboard import DashboardUI

        text = "alpha\nbeta\r\ngamma"
        self.assertEqual(DashboardUI._line_start_offset(None, text, 0), 0)
        self.assertEqual(DashboardUI._line_start_offset(None, text, 1), len("alpha\n"))
        self.assertEqual(DashboardUI._line_start_offset(None, text, 2), len("alpha\nbeta\r\n"))


if __name__ == "__main__":
    unittest.main()
