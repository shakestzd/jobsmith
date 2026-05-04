# jobsmith web

Vite + React 18 + TypeScript port of the design package at
`/Users/shakes/DevProjects/jobsmith/.tmp/design/jobsmith/project/`.

## Dev

```bash
npm install
npm run dev          # starts on http://localhost:5173
npm run typecheck    # tsc --noEmit
npm test             # vitest run (uses msw to mock the API)
npm run build        # type-check + production build into dist/
```

## Backend API

The frontend reads live data from the FastAPI backend (slices 3-5 of
plan-bd67355e). Start it from the project root with `jobsmith api serve`,
which listens on `http://localhost:8000` by default.

### `VITE_API_BASE_URL`

Override the backend URL at build/dev time when the API is served on a
non-default host or port. Copy `.env.example` to `.env.local` and edit:

```bash
cp .env.example .env.local
# .env.local
VITE_API_BASE_URL=http://localhost:8000
```

The variable is consumed by `web/src/api/client.ts` via `import.meta.env`;
when unset (or empty), the client falls back to `http://localhost:8000`.
Trailing slashes are trimmed automatically.

## Structure

```
web/
├── index.html              Vite entry (loads Inter + JetBrains Mono, mounts #root)
├── vite.config.ts          @vitejs/plugin-react, port 5173
├── tsconfig.json           strict, jsx: react-jsx, target ES2022
└── src/
    ├── main.tsx            App entry (placeholder; finalised in Phase 3)
    ├── styles.css          Design tokens + 3 themes + components — copied verbatim from design
    ├── types.ts            Shared TypeScript types (IconName, SampleApp, TweakValues, ViewName, ...)
    ├── tweaks-panel.tsx    Floating Tweaks panel + useTweaks hook
    └── app/
        ├── shared.tsx      Icon, Badge, StatusBadge, Code, SAMPLE_APPS, SAMPLE_BULLETS
        └── chrome.tsx      Sidebar, Topbar
```

Phase 2/3 agents will add `dashboard.tsx`, `new.tsx`, `application.tsx`,
`master.tsx`, `views.tsx`, and replace `main.tsx` with the real shell.

## Conventions (please honour these in subsequent phases)

- ES module `export` only — never `Object.assign(window, ...)`.
- Function components: `function Foo(props: FooProps) { ... }`. No `React.FC`.
- Props typed via `interface` or `type`; avoid `any`.
- `useTweaks` returns a tuple `[values, setTweak]` — destructure as an array.
- Class names, DOM structure, and CSS interactions must match the design pixel-perfectly.
- Files stay <500 lines; functions <50 lines.
