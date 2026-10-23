# TGM demo kit

Three small document libraries, each with two versions, and one script
that loads them, rehearses the questions and asks them live.

| Library | Version 1 | Version 2 (the change) |
|---|---|---|
| `contracts` (made up) | A services agreement with Northwind Health, long enough that TGM cuts it into passages; a shorter one with Contoso Pharmacy; a customer list; an NDA; and `board-minutes.pdf`, a placeholder the upload skips by name | Amendment No. 1 to the Northwind agreement: notice to terminate 30 to 60 days, data deletion 90 to 30 days, liability cap one to two times the fees |
| `regulation` (public, plus one made-up file) | SEC Rule 15c6-1 as it stood before the T+1 amendment, and a made-up settlement desk procedure | The rule as amended (one business day), and the Federal Register notice of the amendment |
| `help-center` (made up) | Six help articles for a software product | The refund policy: monthly plans refundable within 14 days instead of 30 |

Every file starts with a one-line label saying whether it is made up or
public, and where public text came from.

## Where the public text comes from

- `17-cfr-240.15c6-1.md`: 17 CFR 240.15c6-1 from the eCFR point-in-time
  service (https://www.ecfr.gov), version 1 as in force on March 5, 2023
  and version 2 as in force from May 5, 2023.
- `amendment-notice-88-fr-13872.md`: the SUMMARY, DATES and first
  SUPPLEMENTARY INFORMATION paragraph of the final rule "Shortening the
  Securities Transaction Settlement Cycle", 88 FR 13872 (March 6, 2023),
  https://www.federalregister.gov/d/2023-03566.

Both are U.S. government works, not copyrighted in the U.S. The wording
is unchanged: only line breaks were rejoined, footnote markers removed
and the plain-text quote marks turned into quotation marks.

## Run it

You need Python 3.9 or newer, `httpx`, and a key that may create
graphs. The script uses the client in this repository, not a copy
installed on your computer.

```bash
export SEARCHCANDY_API_KEY=sc_live_...
python3 examples/demo/demo.py status       # is the service answering? what is loaded?
python3 examples/demo/demo.py load         # version 1, then version 2, of each library
python3 examples/demo/demo.py check        # ask every scripted question: PASS or FAIL
python3 examples/demo/demo.py questions    # the scripted questions and their commands
python3 examples/demo/demo.py ask contracts notice-now
python3 examples/demo/demo.py ask contracts notice-now --before-update
python3 examples/demo/demo.py timeline contracts
```

`load --step 1` and `load --step 2` load the two versions separately:
load step 1 a day before you show it and step 2 on the day, and asking
"as of yesterday" returns version 1. Loading again never duplicates
anything: a step that is already in the graph's timeline is skipped.

`check` passes a question only when the expected files and words come
back, from the expected version, every passage matches the local file
word for word, and asking again returns the same passages; its note
says where TGM ranked each needed file.

`ask` shows what your AI would receive: the files in the order TGM
ranks them, each file's passages in that file's own reading order. It
marks the passage that holds a scripted question's answer (or the words
you give `--find`, for a question made up on the spot) and shows the
other passages on `--all`.

The tests in `tests/test_demo_offline.py` run the script against a fake
service on your computer; only `check` against the real service shows
what TGM itself returns (on 2026-10-23: all 14 questions PASS).
