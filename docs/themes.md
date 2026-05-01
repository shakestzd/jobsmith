# Per-application theming

Each rendered application page (`<app>/index.html`) picks up a per-company SCSS theme. The resolution chain (high → low) is:

1. **App-level override** — `applications/<slug>/theme.scss` if the user committed a custom theme for this single application.
2. **Curated company SCSS** — `templates/themes/companies/<slug>.scss` from the bundled jobsmith library (this directory).
3. **Default fallback** — `templates/themes/default.scss`.

The resolved theme is symlinked into the application directory as `<app>/theme.scss`. The per-app `_quarto.yml` references it via `format.html.theme: [cosmo, theme.scss]`.

The slug is derived from `jd-parsed.json.company` using the rule documented in `src/jobsmith/research.py::slugify`:

| Input               | Slug              |
| ------------------- | ----------------- |
| `Schneider Electric` | `schneider-electric` |
| `PwC`                | `pwc` |
| `Microsoft Corp.`    | `microsoft-corp` |
| `Smith & Wesson`     | `smith-wesson` |

## SCSS contract

Every curated theme MUST define these four variables:

| Variable                | Purpose |
| ----------------------- | ------- |
| `$jobsmith-primary`     | Primary brand color (headings, accents). |
| `$jobsmith-primary-bg`  | Light tint of primary (callout backgrounds). |
| `$jobsmith-accent-color`| Secondary accent color (borders, highlights). |
| `$jobsmith-accent-bg`   | Light tint of accent (blockquote backgrounds). |

Beyond the variables, a theme should ship a small set of cosmetic rules — usually a `h1, h2` border-left, blockquote styling, and a `.callout-tip` border. Keep themes under ~30 lines; the goal is a brand-flavored accent, not a redesign.

## Currently shipped themes

| Slug                  | Company           | Primary color   |
| --------------------- | ----------------- | --------------- |
| `schneider-electric`  | Schneider Electric| `#3DCD58` (green) |
| `google`              | Google            | `#4285F4` (blue)  |
| `microsoft`           | Microsoft         | `#0078D4` (blue)  |
| `pwc`                 | PwC               | `#DC6B2F` (orange)|
| `netflix`             | Netflix           | `#E50914` (red)   |
| `anthropic`           | Anthropic         | `#CC785C` (clay)  |
| `stripe`              | Stripe            | `#635BFF` (purple)|
| `openai`              | OpenAI            | `#10A37F` (green) |
| `apple`               | Apple             | `#1D1D1F` (near-black) |
| `amazon`              | Amazon            | `#FF9900` (orange)|

## Contributing a new theme

1. Identify the company's two primary brand colors. Prefer the values published on their brand-guidelines page (search `<company> brand guidelines color`). Avoid eyedropping — vendor logos often render in compressed sRGB.
2. Pick a primary (used for headings) and an accent (used for blockquotes, links). Companies with bright primaries (Netflix red) usually need a calmer accent; companies with dark primaries (Apple near-black) can take a saturated accent.
3. Compute light-tint backgrounds. ~10% saturation, ~95% lightness works for most cases. `https://hslpicker.com` is fine.
4. Copy `templates/themes/companies/google.scss` as a starting template; replace the four variables and update the comment header.
5. Verify the slugify rule. The filename must match `slugify(<company name>)` exactly. If the company has multiple slug variants (e.g. "Schneider Electric" vs. "schneider electric SA"), pick the canonical one and let `_resolve_theme` fall back to default for the others — better than maintaining N near-duplicate files.
6. Render an application against the new theme to visually QA. The page should feel branded without being garish; if your accent border is fighting the body text, dial down the accent or swap primary/accent.

If the library grows past ~25 themes, we'll move it to a separate `jobsmith-themes` repo so consumers can pin a version. For now, themes ship inside jobsmith.

## When NOT to add a curated theme

Most applications will use the default theme — the curated set is for companies the user applies to repeatedly. Don't add a theme for a one-off application; the app-level override (`<app>/theme.scss`) is the right escape hatch for those.
