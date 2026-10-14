# Offline political disclosure fixtures

The PDFs are small excerpts of public US government disclosures. Pages were
copied with PDFium, preserving the original text or scanned image; no source
requests are made by tests. `senate.html` is a synthetic document with invented
transactions following the official table headings.

| Fixture | Official source | Original pages |
|---|---|---|
| `electronic.pdf` | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20026590.pdf | 1–2 |
| `scanned.pdf` | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/8220750.pdf | 2 |
| `mccaul.pdf` | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/8220754.pdf | 2 |
| `oge.pdf` | https://www.whitehouse.gov/wp-content/uploads/2026/09/President-Donald-J.-Trump-Periodic-Transaction-Report-09.08.26.pdf | 2 |

`oge-equity.pdf` copies page 2 of https://www.whitehouse.gov/wp-content/uploads/2026/08/President-Donald-J.-Trump-Periodic-Transaction-Report-08.12.26.pdf. It preserves the original orientation and an ink-damaged row for the OCR regression checks.

`electronic-spanning.pdf` copies pages 2–3 of https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf. Asset and amount cells continue before the next transaction.

Official form references:

- House Clerk financial disclosures: https://disclosures-clerk.house.gov/
- Senate eFD: https://efdsearch.senate.gov/
- OGE 278-T: https://www.oge.gov/web/oge.nsf/Resources/OGE+Form+278-T

Synthetic fixtures exercise parser behavior, and are not an accuracy audit
against government disclosures.
