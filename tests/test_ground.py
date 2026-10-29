"""Grounding check (copied ABI rules): an invented number or member is caught and masked; backed ones pass."""
import json
import subprocess
import sys
from pathlib import Path

import ground

ROWS = [{"columns": ["category", "views"], "rows": [["Music", 1234567], ["Gaming", 98765]]}]


def test_invented_number_is_caught():
    bad = ground.check("Music has 1,234,567 views and Gaming 120,000.", ROWS, spans=True)
    assert bad["numbers"] == ["120,000"]
    assert "[not verified]" in ground.mask("Music has 1,234,567 views and Gaming 120,000.", bad)


def test_backed_numbers_rounding_and_question_pass():
    assert ground.check("Music leads with 1.2 million views; Gaming has 98,765. Top 2 shown.", ROWS,
                        asked="top 2 categories") == {"numbers": [], "names": []}


def test_invented_member_is_caught():
    assert ground.check("The top category is Atlantis with 98,765 views.", ROWS)["names"] == ["Atlantis"]


def test_cli_skips_answers_without_numbers():
    run = lambda req: json.loads(subprocess.run([sys.executable, str(Path(ground.__file__))], input=json.dumps(req),
                                                capture_output=True, text=True, check=True).stdout)
    assert run({"answer": "Atlantis is great.", "results": []}) == {"numbers": [], "names": []}
    out = run({"answer": "We have 42 videos.", "results": []})
    assert out["numbers"] == ["42"] and "42" not in out["masked"]
