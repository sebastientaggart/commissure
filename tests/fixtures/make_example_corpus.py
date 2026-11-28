"""Generate the example solution's corpus: `commissure/scaffold/example/intake/comments/`.

Fictional public comments on a fictional proposal (the Alder Creek library-hours levy). No
real place, person, or organization. Generated, never hand-edited: change this script and
rerun it, then update the counts in the example's tests and `recipe/AGENTS.md`.

    uv run python tests/fixtures/make_example_corpus.py
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from docx import Document
from make_probe_corpus import LETTER, Pdf, full_page, text_block

OUT = Path(__file__).resolve().parents[2] / "commissure/scaffold/example/intake/comments"

FIXED = dt.datetime(2026, 1, 1)
HEADER = "Public comment on Proposal 7: evening and Sunday hours at the Alder Creek Library"

# (file stem, paragraphs). Positions and topics follow recipe/AGENTS.md in the example.
WEB = {
    "c01": ["I support the proposal. Evening hours would let me visit after my shift ends."],
    "c02": [
        "I oppose this levy. The tax is too high for families on fixed incomes.",
        "The current hours are fine for most of us.",
    ],
    "c03": ["Please approve Proposal 7. My kids need a quiet place for homework on Sunday."],
    "c04": ["I am against the change. The library staff are already stretched thin."],
    "c05": [
        "I support longer hours, but only if parking near the branch is fixed first.",
        "Right now the lot is full by four o'clock.",
    ],
    "c06": ["Please reject this. The cost per household has not been explained."],
    "c07": ["I am in favor. Students at the high school have nowhere to study in the evening."],
    "c08": ["I support the plan. Sunday afternoons are the only time I can come in."],
    "c09": ["The library is a good thing. I have no strong view on the hours."],
    "c10": ["I oppose the proposal as written, and I support it if the levy is cut in half."],
    "c11": ["Please approve it. More evening hours means more staff jobs in town."],
    "c12": ["I am against any new tax this year."],
    "c13": ["I support this. Homework help on weekday evenings would change my son's year."],
    "c14": ["I oppose the plan. Parking is already a problem on Sunday mornings for the church."],
    "c15": ["I am in favor of the proposal, and I would pay the levy gladly."],
    "c16": ["Please approve the new hours. Students and night-shift workers both need this."],
    "c17": ["I support it."],
    "c18": ["Please reject Proposal 7. Hire more staff for the current hours before adding more."],
}
DOCX = {
    "c19": [
        "I support the proposal.",
        "Our book club meets in the evening and has outgrown the community room schedule.",
    ],
    "c20": ["I am against the levy. The cost is not justified by the attendance numbers."],
}
# A two-page letter whose position is on page 2, so its evidence names a page.
PDF_LETTER = [
    [
        "Dear members of the library board,",
        "I have used the Alder Creek branch for twenty years and I want to explain",
        "how the current schedule works for my household before giving my view.",
        "We visit on Saturday mornings, and the staff there are always helpful.",
    ],
    ["Having thought it over, I support the proposal.", "Sunday hours would help my neighbours."],
]
PDF_SHORT = ["I oppose the proposal. The levy would fall hardest on renters."]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    # Web-form submissions, as the form's export saves them: one small HTML page each.
    for stem, paras in WEB.items():
        body = "".join(f"<p>{p}</p>" for p in paras)
        page = f"<!doctype html><html><body><main><h1>{HEADER}</h1>{body}</main></body></html>\n"
        (OUT / f"{stem}.html").write_text(page, encoding="utf-8")
    for stem, paras in DOCX.items():
        doc = Document()
        props = doc.core_properties  # fixed, so a rerun writes the same bytes
        props.author = props.last_modified_by = "Example"
        props.created = props.modified = FIXED
        doc.add_paragraph(HEADER)
        for p in paras:
            doc.add_paragraph(p)
        doc.save(OUT / f"{stem}.docx")

    pdf = Pdf()
    f = {"F1": pdf.helvetica}
    pdf.page(text_block(72, 720, [HEADER, "", *PDF_LETTER[0]]), fonts=f)
    pdf.page(text_block(72, 720, PDF_LETTER[1]), fonts=f)
    pdf.write(OUT / "c21.pdf")

    pdf = Pdf()
    pdf.page(text_block(72, 720, [HEADER, "", *PDF_SHORT]), fonts={"F1": pdf.helvetica})
    pdf.write(OUT / "c22.pdf")

    # A scanned letter: an image-only page with no text layer. The readability gate sets it
    # aside (OCR is opt-in and needs Tesseract), so it never reaches the recipe's stages.
    pdf = Pdf()
    pdf.page(full_page(LETTER), images={"Im1": pdf.image(306, 396)})
    pdf.write(OUT / "c23-scan.pdf")

    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT}")


if __name__ == "__main__":
    main()
