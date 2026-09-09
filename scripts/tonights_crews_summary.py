"""Render the GitHub Actions job summary for the tonight's-crews workflow.

Kept out of the workflow YAML so the summary logic is testable on its own and
the YAML holds no nested heredoc. Prints markdown to stdout; the workflow
redirects it into $GITHUB_STEP_SUMMARY. Read-only.
"""

import json
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tonights-crews.json")


def main():
    print("## Tonight's crews")
    print("")
    if not os.path.exists(OUT):
        print("No file was written -- the fetch failed, so the previously committed")
        print("`data/tonights-crews.json` (if any) is untouched.")
        return 0
    with open(OUT, "r", encoding="utf-8") as fh:
        d = json.load(fh)

    print("| Field | Value |")
    print("| --- | --- |")
    print("| Date | %s |" % d.get("date"))
    print("| Games | %s |" % d.get("game_count"))
    print("| Unmatched officials | %d |" % len(d.get("unmatched") or []))
    print("| Committed | %s |" % (os.environ.get("COMMITTED") or "false"))
    print("")

    games = d.get("games") or []
    if not games:
        print("_No games on this date. The file is valid and empty; the home page keeps")
        print("its server-rendered fallback (most recent game day on record)._")
    for g in games:
        crew = ", ".join((c.get("name") or "") + ("" if c.get("slug") else " *(unmatched)*")
                         for c in g.get("crew") or [])
        line = "- **%s @ %s** - %s" % (g.get("away"), g.get("home"), crew)
        if g.get("crew_note"):
            line += " _(%s)_" % g["crew_note"]
        print(line)

    unmatched = d.get("unmatched") or []
    if unmatched:
        print("")
        print("### Unmatched officials")
        print("")
        print("Rendered without a link, not guessed. Add a row to")
        print("`data/referee_identity_overrides.csv` to resolve, or leave for the")
        print("canonical-id round (NBA person ids are in `official_code_index`).")
        print("")
        print("| Name | official_code | Same surname on file |")
        print("| --- | --- | --- |")
        for u in unmatched:
            print("| %s | %s | %s |" % (u.get("name"), u.get("official_code") or "-",
                                        ", ".join(u.get("candidates") or []) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
