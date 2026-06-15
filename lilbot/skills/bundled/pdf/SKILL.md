---
name: pdf
description: Read, extract, split, merge, rotate, watermark, fill, OCR, or create PDF files.
mode: inline
---
# PDF

Use this skill for PDF work.

Procedure:

- Inspect file existence, size, and page count when possible.
- Extract text before summarizing.
- For edits, keep the original file and write a new output path.
- Verify output page count and text extraction after conversion.
- If OCR or PDF libraries are unavailable, state the missing dependency and
  provide the exact next command the user can run.

User task: {{args}}
