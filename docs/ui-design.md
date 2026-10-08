# Knowgrain Web UI design direction

Date: 2026-10-08

## Current UI audit

The web app is a local-first knowledge workspace, not a marketing site. Its main work areas are Sources, Wiki, Questions, and Vault settings. Sources uses a three-column service/sidebar, source list, and revision inspector layout. Wiki keeps the page list beside the Markdown editor and preview, then exposes backlinks, LightRAG entities, generation review, and evidence. Questions pairs a saved-query history with answers and source citations. Vault selection is a focused modal. Source lifecycle, index maintenance, file recovery, reconciliation, and evidence are contextual panels.

The current system uses an Apple/system font stack and the Knowgrain grain mark, with muted indigo (`#5265c9`) on gray-blue surfaces. The UI is mostly light-only and opaque. Its small radii and compact type vary between workspaces; panel colors and status colors drift between blue-gray and warm beige. Motion is limited to the loading spinner. The existing navigation, Chinese labels, form order, file workflows, editor/preview split, and source/evidence links are useful and will stay intact. SEO and public metadata do not apply to this local workspace.

## Visual direction

Reading this as a redesign of a local knowledge workspace for people organizing and verifying source material, with calm Apple system aesthetics and a cold silver palette. The web treatment is a restrained frosted-glass approximation; it is not Apple's native Liquid Glass implementation.

| Dial | Value | Design effect |
| --- | ---: | --- |
| `DESIGN_VARIANCE` | 6 | Keep familiar workspace structure, add a more deliberate depth and grouping rhythm. |
| `MOTION_INTENSITY` | 4 | Use brief opacity/position transitions for hover, focus, selection, and panel entry. |
| `VISUAL_DENSITY` | 5 | Preserve useful metadata while increasing text size, spacing, and panel separation. |

The shared system uses cold silver neutrals, one softened Apple-blue accent, system fonts, consistent 16–20px card corners, and 10–12px control corners. Glass surfaces identify navigational and contextual hierarchy. Editors, inputs, tables, and evidence quotations remain solid. Light and dark palettes follow the system preference. `prefers-reduced-transparency` and `prefers-reduced-motion` receive explicit fallbacks. Focus rings, status labels, and errors remain visible without color alone.

UI controls use Phosphor's maintained React icon set through per-icon package entry points, while the existing Knowgrain grain mark remains intact. The per-icon imports keep unused icons out of the application bundles.

## Scope and preservation

The redesign applies to Sources, Wiki, Questions, Vault settings, generation and review, evidence, entity links, source lifecycle, index maintenance, reconciliation, and file operations. It preserves routes and workspace names, logo treatment, copy, API contracts, field names and order, action ordering, empty/loading/error/retry states, and all backend behavior. Responsive layouts collapse the existing columns explicitly for tablet and mobile widths.

## Practical QA

`npm run build` in `apps/web` passed, including `tsc --noEmit`; `node --test checks/*.test.mjs` passed all 24 contract tests. The production build still emits a Wiki JavaScript chunk above Vite's 500 kB advisory threshold.

Production-browser layout QA used synthetic API responses. Sources and Questions passed light and dark theme checks at 390, 768, 1024, and 1440 pixels with no horizontal overflow and readable meaningful text. Wiki's populated editor and preview passed the same four widths and both themes; its CodeMirror content and gutters used the semantic light/dark colors. Vault and Evidence dialogs fit at 390, 768, and 1440 pixels in both themes with readable contrast. The Wiki create form opened and fit at 390, 768, and 1440 pixels; cancel closed it without creating a page, leaving the original row count unchanged.

Lighthouse ran in headed Chrome against the production preview using `--preset=desktop --throttling-method=provided` after two headless attempts returned `NO_FCP` while the browser UI rendered. The successful run tested only the offline Sources initial/error state at a 1440×960 viewport: performance 100, accessibility 100, FCP/LCP 284.63 ms, CLS 0, and TBT 0. INP was not measured. This score does not cover populated/API-backed user flows. The backend was unavailable, so live upload, Vault persistence, Wiki save/generation, Questions submission, and evidence retrieval remain unverified.

Reproduction command:

```sh
npx --yes --package lighthouse lighthouse http://127.0.0.1:4173/ --chrome-flags='--window-size=1440,960 --disable-gpu --no-proxy-server' --preset=desktop --throttling-method=provided --only-categories=performance,accessibility --output=json --output-path=output/playwright/lighthouse-desktop.json --quiet
```

The reduced-transparency browser emulation originally exposed a cascade issue: global tokens became opaque, but lazy-loaded Wiki pane rules still applied blur. After the fallback declarations were strengthened, runtime checks confirmed that Wiki panes in light and dark themes and Questions, Evidence, Sources, and Vault surfaces in dark theme use opaque canvases with no backdrop blur; dialog backdrops also lose blur. With reduced motion enabled, the app shell animation duration resolves to `0.00001s`. Wiki create/cancel was also exercised: opening and cancelling the form leaves the page count unchanged.

Local screenshots and the Lighthouse report are kept under `output/playwright/` as untracked QA artifacts (for example, `sources-light-v2.png`, `sources-dark.png`, `wiki-light-v2.png`, `wiki-dark.png`, and `lighthouse-desktop.json`); generated artifacts are not part of the source change. Keyboard traversal and live user flows remain for follow-up when the API is available. The backend was unavailable during this UI pass, so upload, Vault persistence, Wiki save/generation, Questions submission, and evidence retrieval have not been verified against live services.
