---
name: board-pack
description: Board packs - what management takes to a board or leadership review - one-pager, PDF/PPTX documents, data extracts and a silent explainer video, checked before they go out. Use for "board pack", "board deck", "one-pager", "for the board", "export for leadership", "explainer video".
---
# Board pack
1. Read the vault (abi_vault) for the definitions the pack talks about, and the person's brief: the meeting, the
   audience, the period, what they want to show. Ask nothing you can find; state your assumptions.
2. Pick the views. Prefer the existing reports (list_reports). A one-pager, if asked: one ABI report page
   (save_report) with at most 4 KPI tiles, 2 charts and one text visual that says the period and the caveats.
3. Check before anything renders, with figures from the rows:
   - the period is complete (no partial month at the end) and the same across the pack's views, or say so;
   - totals agree where views overlap (a KPI and the chart it summarises);
   - every measure follows the vault (how_calculated), and channels are grouped by ID, not by name only;
   - the caveats (demo data, incomplete months, differing populations) are written in the pack's text visual or notes.
4. Write the notes (Markdown commentary: what moved, why it matters, what is open). Every number must appear in a
   view of the pack's reports, exactly or in the compact form (1.3m, 17.9bn); abi_pack refuses the pack otherwise.
5. abi_pack with the items: the documents (pdf; pptx and several pages in one file when this ABI renders them; it
   says when it can't), the data extracts (csv, xlsx) of the tables the board will ask for, and a video of the
   headline views (theme paper unless the person asks for ink). Captions are optional: the default ones come from
   the rows; your own must keep to the rows' numbers.
6. Never send. If the person wants it sent, list send_email (or a flow) in pending_confirmations, with the pack id.
7. Hand off: done [{kind: "pack", id}], the checks with figures, and what ABI could not render.
