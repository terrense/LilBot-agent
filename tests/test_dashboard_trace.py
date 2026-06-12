from __future__ import annotations

import unittest
from types import SimpleNamespace

from lilbot.tui.dashboard import (
    LILBOT_AGENT_LOGO_COMPACT_ROWS,
    LILBOT_AGENT_LOGO_ROWS,
    _clip_line,
    _format_markdown_tables,
    _highlight_trace_line,
    _summarize_interim_text,
    _summarize_tool_output,
    DashboardUI,
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
        art = "\n".join(LILBOT_AGENT_LOGO_ROWS)
        self.assertIn("██████", art)
        self.assertIn("-", LILBOT_AGENT_LOGO_ROWS[0])
        self.assertGreaterEqual(max(len(row) for row in LILBOT_AGENT_LOGO_ROWS), 86)
        self.assertEqual(len(LILBOT_AGENT_LOGO_COMPACT_ROWS), 3)
        self.assertLessEqual(len(_clip_line(LILBOT_AGENT_LOGO_ROWS[0], 30)), 30)

    def test_markdown_tables_render_as_aligned_box_tables(self):
        source = "\n".join(
            [
                "| 项目 | 估算 (USD) |",
                "| --- | --- |",
                "| 国际机票（往返） | $1,200-1,800 |",
                "| 总计 | $3,500-5,000 |",
            ]
        )

        rendered = _format_markdown_tables(source)
        lines = rendered.splitlines()

        self.assertTrue(lines[0].startswith("┌"))
        self.assertTrue(lines[-1].startswith("└"))
        self.assertIn("│ 项目", rendered)
        self.assertEqual({line.count("│") for line in lines if line.startswith("│")}, {3})

    def test_line_start_offset_for_scroll_cursor(self):
        text = "alpha\nbeta\r\ngamma"
        self.assertEqual(DashboardUI._line_start_offset(None, text, 0), 0)
        self.assertEqual(DashboardUI._line_start_offset(None, text, 1), len("alpha\n"))
        self.assertEqual(DashboardUI._line_start_offset(None, text, 2), len("alpha\nbeta\r\n"))

    def test_permission_popup_is_separate_render_text(self):
        ui = SimpleNamespace(pending_permission="run shell command: " + "x" * 220)

        fragments = DashboardUI._permission_popup(ui)
        plain = "".join(text for _style, text in fragments)

        self.assertIn("PERMISSION GATE", plain)
        self.assertIn("allow once", plain)
        self.assertIn("always deny", plain)
        self.assertIn("Display shortened", plain)

    def test_slash_suggestion_popup_renders_model_commands(self):
        ui = object.__new__(DashboardUI)
        ui.input = SimpleNamespace(text="/mo")
        ui.slash_hidden_for_text = ""
        ui.slash_selection = 0
        ui.pending_permission = None

        fragments = ui._slash_suggestions_popup()
        plain = "".join(text for _style, text in fragments)

        self.assertIn("COMMAND DECK", plain)
        self.assertIn("/model", plain)
        self.assertIn("/models", plain)
        self.assertIn("Tab accept", plain)

    def test_slash_table_routes_to_command_popup(self):
        ui = object.__new__(DashboardUI)
        ui.route_slash_to_popup = True
        ui.command_popup_title = ""
        ui.command_popup_lines = []
        ui.command_popup_error = False
        ui._refresh = lambda: None

        ui.table("Models", ["Model", "Status"], [("deepseek-v4-flash", "current")])

        self.assertEqual(ui.command_popup_title, "Models")
        self.assertTrue(any("deepseek-v4-flash" in line for line in ui.command_popup_lines))
        self.assertFalse(ui.command_popup_error)


if __name__ == "__main__":
    unittest.main()
