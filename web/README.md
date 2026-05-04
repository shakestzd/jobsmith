# jobsmith web

Vite + React 18 + TypeScript port of the design package at
`/Users/shakes/DevProjects/jobsmith/.tmp/design/jobsmith/project/`.

## Dev

```bash
npm install
npm run dev          # starts on http://localhost:5173
npm run typecheck    # tsc --noEmit
npm run build        # type-check + production build into dist/
```

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
