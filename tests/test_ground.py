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


def test_clock_minutes_are_no_figure():
    """'every Monday at 8' became 'at 08:[not verified]': the minutes of a time were checked as a figure (REPORT #43)."""
    assert ground.check("The flow runs every Monday at 08:00 UTC.", ROWS, asked="every Monday at 8") == {"numbers": [], "names": []}
    assert ground.check("The flow runs every Monday at 09:00 UTC.", ROWS, asked="every Monday at 8")["numbers"] == ["09"]



def test_a_link_label_opens_like_a_sentence():
    """'[Download your dashboard PDF](…)' came back as '[[not verified] your dashboard PDF]'."""
    assert ground.check("[Download your dashboard PDF](/api/artifacts/p/x.pdf)", ROWS)["names"] == []
    assert ground.check("See [Atlantis Monthly](/x) with 98,765 views.", ROWS)["names"] == ["Atlantis Monthly"]  # mid-sentence: still a name


def test_the_other_side_of_a_yes_no_column_is_no_invented_name():
    """Release run 30 Oct: "Nonmembers" for `loyalty_member` = false was masked, and with it the plain word in prose."""
    res = [{"columns": ["loyalty_member", "average_ticket"], "rows": [[True, 5.6544], [False, 5.6574]]}]
    ans = "Members and nonmembers.\n\n| Loyalty status | Average ticket |\n|---|---:|\n| Members | 5.6544 |\n| Nonmembers | 5.6574 |\n| Non-members | 5.6574 |"
    assert ground.check(ans, res, fields={"dim_customers.loyalty_member"})["names"] == []
    assert "Nonbuyers" in ground.check("| Loyalty status | Average ticket |\n|---|---:|\n| Nonbuyers | 5.6574 |", res, fields={"dim_customers.loyalty_member"})["names"]


def test_the_plural_of_a_name_the_person_typed_is_theirs():
    """Release run 31 Oct: asked "every Monday at 8", the answer's "Mondays at 08:00" came back "[not verified] at 08:00"."""
    asked = "email me a weekly summary every Monday at 8"
    assert ground.check("Runs on **Mondays at 08:00 UTC**.", [], asked=asked)["names"] == []
    assert ground.check("Runs on **Tuesdays at 08:00 UTC**.", [], asked=asked)["names"] == ["Tuesdays"]


def test_a_status_word_opening_a_label_value_is_no_name():
    """Live run 31 Oct: "- **Status:** Saved **disabled**" came back "[not verified] **disabled**"."""
    for w in ("Saved", "Created", "Currently", "Kept", "Paused"):
        assert ground.check(f"- **Status:** {w} **disabled**; no email sent.", [])["names"] == [], w
    assert ground.check("- **Top region:** Narnia, by revenue.", [])["names"] == ["Narnia"], "a data name after a label is still checked"
    assert ground.check("- **Tier:** Platinum", [])["names"] == ["Platinum"], "data-like values stay checked"


def test_a_figure_in_a_definitions_title_grounds_as_written():
    """1.1 #6: answers about the reports themselves came back half-masked: "28-day" from a visual title is the
    project's own words. A figure of the data, an ID's digits or a rounded figure still need a result row."""
    rep = {"reports": [{"id": "daily_revenue_28d", "title": "Revenue over the last 28 days", "pages": [{"layout": {"w": 12}}]}]}
    check = ground.check
    assert check("The page shows revenue over the last 28 days.", [rep])["numbers"] == []
    assert check("Revenue was 17,327,168.70 over 12 weeks.", [rep])["numbers"] == ["17,327,168.70", "12"]
    assert check("It covers 30 days.", [rep])["numbers"] == ["30"]
