# Bogdan Iancu — academic homepage

A single-file, zero-build personal researcher page (`index.html`). No frameworks,
no dependencies — it runs anywhere static files are served. Fonts load from Google
Fonts; everything else is inline. Dark and light themes follow the visitor's system
setting.

## Deploying to GitHub Pages

This folder is staged inside the `saari` repo for convenience. To publish it as your
personal site, the cleanest home is a dedicated repository named after your GitHub
account so it serves from the root domain:

### Option A — root user site (recommended) → `https://biancu.github.io`

1. Create a new **public** repository named exactly `biancu.github.io`.
2. Copy `index.html` (and `.nojekyll`) into the root of that repository and push to `main`.
3. In the repo's **Settings → Pages**, set **Source = Deploy from a branch**,
   **Branch = `main` / `/ (root)`**.
4. The site goes live at `https://biancu.github.io` within a minute or two.

### Option B — project site from this repo → `https://biancu.github.io/saari`

1. In **Settings → Pages**, set **Source = Deploy from a branch**,
   **Branch = `<this branch>` / `/homepage`** (once merged, use the default branch).
2. The site serves from `https://biancu.github.io/saari/`.

### Custom domain (optional)

Add a `CNAME` file containing your domain (e.g. `bogdaniancu.com`) and configure the
DNS records shown in **Settings → Pages**.

## Editing

Everything is in `index.html`:

- **Content** lives in plain, labelled sections (`About`, `Research`, `Experience`, …).
- **Colours, fonts, spacing** are CSS custom properties in the `:root` block at the top.
- To add a **photo**, drop an image in this folder and reference it from the hero.

## Notes

- All employment and grant entries use **years only**; no funding amounts are shown.
- The publication list is summarised — ORCID is linked as the authoritative source.
- The contact email in the page is a placeholder based on the standard
  `firstname.lastname@abo.fi` convention — **verify or replace it** before publishing.
