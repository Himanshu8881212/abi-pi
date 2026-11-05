"""1.2 (owner, 5 Nov 2026): ABI's pages and exports load no font from Google (a browser asking fonts.googleapis.com
for every visitor is a GDPR question in the EU); Inter Tight and IBM Plex Mono ship in scripts/fonts under the SIL
Open Font License, embedded in the pages."""
import base64
import re
from pathlib import Path

import app
import render

ROOT = Path(__file__).resolve().parent.parent


def test_no_page_or_export_asks_google_for_fonts():
    for f in ("scripts/app.py", "scripts/render.py", "scripts/pack.py", "scripts/video.py"):
        if (ROOT / f).exists():
            assert not re.search(r"fonts\.(googleapis|gstatic)\.com", (ROOT / f).read_text()), f
    assert "fonts.googleapis" not in app.SIGNIN and render.FONTS in app.SIGNIN
    assert render.FONTS in render.shell("t", "s", "<p>x</p>")


def test_the_bundled_fonts_are_whole_woff2_files_with_their_licence():
    for family, file in (("Inter Tight", "inter-tight-latin.woff2"), ("IBM Plex Mono", "ibm-plex-mono-latin-400.woff2")):
        raw = (ROOT / "scripts/fonts" / file).read_bytes()
        assert raw[:4] == b"wOF2" and f"font-family:'{family}'" in render.FONTS
        assert base64.b64encode(raw).decode() in render.FONTS
    assert "SIL Open Font License" in (ROOT / "scripts/fonts/OFL.txt").read_text()
