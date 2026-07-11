# CatalogPress — Shopify CSV → PDF catalog website

This is a complete, ready-to-host website. A customer uploads the CSV that
Shopify's **Products → Export** button gives them, types their discount
levels (e.g. `0, 10, 20`), and a couple of minutes later downloads a ZIP
containing one designed, clickable PDF catalog **per discount level** —
old prices crossed out, new prices in the badge.

It is your original catalog script, wrapped in a small web server.

---

## 1. What each file does

| File | In plain English |
|---|---|
| `app.py` | **The backend.** Receives the upload, starts the job, reports progress, serves the download. ~200 lines, heavily commented. |
| `catalog_maker.py` | **The PDF engine** — your original code, reorganised, with discounts, parallel image downloads and progress reporting added. All design settings (colours, fonts, grid size) live at the top of this file. |
| `templates/index.html` | **The page customers see.** Upload form, live progress bar, download button. |
| `requirements.txt` | The list of Python packages the server needs. Hosting providers read this and install them for you. |
| `render.yaml` | Optional one-click setup file for Render.com. |

**How a request flows:** browser sends the CSV to `/generate` → server
answers instantly with a job number and keeps working in the background →
browser asks `/status/<job>` every 1.5 s and fills the progress bar →
when done, `/download/<job>` sends the PDF or ZIP. Files self-delete
after 2 hours.

---

