from __future__ import annotations

import unittest

from lilbot.tui.dashboard import _highlight_trace_line, _summarize_interim_text, _summarize_tool_output


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


if __name__ == "__main__":
    unittest.main()
