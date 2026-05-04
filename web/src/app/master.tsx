// master.tsx — port of design/app/master.jsx
//
// Exports: MasterContent, MarkAnchorsView
// Internal helpers: WorkEditor, BulletEditor (file-local)
//
// Live data flow (slice 6):
//   useMaster() -> MasterPayload { work, skill, education, author }
//   Bullets are derived from the *selected* work entry's `details` array,
//   which the backend models as `list[str | WorkDetailDict]`. String items
//   are treated as un-anchored bullets; dict items expose explicit
//   `anchor: bool` + `bullet: str` fields.
//
// TODO: when a future slice lands a write API for master content, swap the
// in-memory toggle in BulletEditor for a mutation hook + persistence flow.

import { type KeyboardEvent, useMemo, useRef, useState } from 'react';
import {
  useMaster,
  useUpdateMaster,
  useUploadMaster,
  type MasterSection,
} from '../api/hooks';
import { ApiError } from '../api/client';
import type { Author, EducationEntry, SkillEntry, WorkDetail, WorkEntry } from '../api/types';
import { Icon, Badge } from './shared';

// ── Types ────────────────────────────────────────────────────────────────

type MasterTab = 'work' | 'skill' | 'education' | 'author' | 'benchmark';

/** Normalised bullet for the editor UI (independent of the on-disk shape). */
interface UiBullet {
  id: string;
  anchor: boolean;
  text: string;
}

const TABS: Array<[MasterTab, string]> = [
  ['work', 'work.yml'],
  ['skill', 'skill.yml'],
  ['education', 'education.yml'],
  ['author', 'author.yml'],
  ['benchmark', 'benchmark.md'],
];

// ── Derive bullets from a work entry ─────────────────────────────────────

function detailsToBullets(details: WorkDetail[], roleId: string): UiBullet[] {
  return details.map((d, i) => {
    const id = `${roleId}-b${i + 1}`;
    if (typeof d === 'string') {
      return { id, anchor: false, text: d };
    }
    // Object form: { bullet, anchor?, ... }
    const text = typeof d.bullet === 'string' ? d.bullet : '';
    const anchor = d.anchor === true;
    return { id, anchor, text };
  });
}

/** Stable id for a work entry (location+index is the natural composite key). */
function workEntryId(w: WorkEntry, idx: number): string {
  const slugged = `${w.location || 'role'}-${idx}`
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slugged || `role-${idx}`;
}

// ── Internal helpers ─────────────────────────────────────────────────────

interface BulletEditorProps {
  entry: WorkEntry;
  entryId: string;
}

function BulletEditor({ entry, entryId }: BulletEditorProps) {
  const initial = useMemo(
    () => detailsToBullets(entry.details ?? [], entryId),
    [entry, entryId],
  );
  const [bullets, setBullets] = useState<UiBullet[]>(initial);

  const anchorCount = bullets.filter(b => b.anchor).length;

  const toggle = (id: string) => {
    setBullets(bs => bs.map(b => b.id === id ? { ...b, anchor: !b.anchor } : b));
  };

  return (
    <div className="card">
      <div className="card-h">
        <h3>{entry.title || 'role'} · {entry.location || '—'}</h3>
        <span className="sub">{entry.date || ''} · {bullets.length} bullets</span>
        <div className="right">
          <Badge kind="accent">{anchorCount} anchors</Badge>
          <button className="btn ghost sm">add bullet</button>
          <button className="btn ghost sm">save</button>
        </div>
      </div>
      <div style={{ padding: '10px 14px 8px', fontSize: 12, color: 'var(--fg-muted)', borderBottom: '1px solid var(--border)' }}>
        <Icon name="flag" size={11} style={{ verticalAlign: 'middle', color: 'var(--accent)' }} />{' '}
        anchors are bullets <b style={{ color: 'var(--fg)' }}>jobsmith</b> must preserve in every draft (or document a drop-reason).
      </div>
      <div>
        {bullets.length === 0 && (
          <div style={{ padding: 28, textAlign: 'center', color: 'var(--fg-subtle)' }}>
            <div className="mono-sm">no bullets in this role</div>
          </div>
        )}
        {bullets.map((b, i) => (
          <div key={b.id} className={`bullet-row ${b.anchor ? 'is-anchor' : ''}`}>
            <span className="b-num">{String(i + 1).padStart(2, '0')}</span>
            <div>
              <div className="b-text">{b.text}</div>
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <span className={`pill ${b.anchor ? 'active' : ''}`} onClick={() => toggle(b.id)}>
                  {b.anchor ? '⚑ anchor' : 'mark anchor'}
                </span>
                <span className="pill">edit</span>
                <span className="pill">drop reason…</span>
              </div>
            </div>
            <div className="b-actions">
              <button className="btn ghost sm"><Icon name="chevd" size={11} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

interface WorkEditorProps {
  work: WorkEntry[];
}

function WorkEditor({ work }: WorkEditorProps) {
  const ids = useMemo(() => work.map((w, i) => workEntryId(w, i)), [work]);
  const [openId, setOpenId] = useState<string>(ids[0] ?? '');
  const openIdx = ids.indexOf(openId);
  const safeIdx = openIdx >= 0 ? openIdx : 0;
  const openEntry = work[safeIdx];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
      <div className="card" style={{ padding: 8 }}>
        {work.map((w, i) => {
          const rid = ids[i] ?? `role-${i}`;
          const detailsCount = (w.details ?? []).length;
          return (
            <div
              key={rid}
              onClick={() => setOpenId(rid)}
              style={{
                padding: '10px 12px',
                borderRadius: 'var(--radius)',
                background: rid === openId ? 'var(--bg-sunk)' : 'transparent',
                border: rid === openId ? '1px solid var(--border)' : '1px solid transparent',
                cursor: 'pointer',
                marginBottom: 4,
              }}
            >
              <div style={{ fontWeight: 500, fontSize: 13.5 }}>{w.title || 'role'}</div>
              <div style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{w.location || '—'}</div>
              <div className="mono-sm" style={{ color: 'var(--fg-subtle)', marginTop: 2 }}>{w.date || ''} · {detailsCount} bullets</div>
            </div>
          );
        })}
        <div style={{ padding: 8 }}>
          <button className="btn ghost sm" style={{ width: '100%', justifyContent: 'center' }}>
            <Icon name="plus" size={11} /> add role
          </button>
        </div>
      </div>

      {openEntry ? (
        <BulletEditor key={ids[safeIdx]} entry={openEntry} entryId={ids[safeIdx] ?? 'role-0'} />
      ) : (
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <div className="mono-sm">no work entries in master/work.yml</div>
        </div>
      )}
    </div>
  );
}

// ── Loading + error helpers ──────────────────────────────────────────────

interface QueryStatusProps {
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
  label: string;
}

function QueryStatus({ isLoading, isError, refetch, label }: QueryStatusProps) {
  if (isLoading) {
    return (
      <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
        <span className="mono-sm">loading {label}…</span>
      </div>
    );
  }
  if (isError) {
    return (
      <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--danger, #c43)' }}>
        <div className="mono-sm" style={{ marginBottom: 8 }}>failed to load {label}</div>
        <button className="btn ghost sm" onClick={refetch}>retry</button>
      </div>
    );
  }
  return null;
}

// ── Exported components ──────────────────────────────────────────────────

export function MasterContent() {
  const [tab, setTab] = useState<MasterTab>('work');
  const query = useMaster();
  const data = query.data;

  const tabCounts: Record<MasterTab, string> = useMemo(() => {
    if (!data) {
      return { work: '—', skill: '—', education: '—', author: '—', benchmark: 'tone reference' };
    }
    const totalBullets = data.work.reduce(
      (acc, w) => acc + (w.details?.length ?? 0),
      0,
    );
    return {
      work: `${data.work.length} roles · ${totalBullets} bullets`,
      skill: `${data.skill.length} groups`,
      education: `${data.education.length} entries`,
      author: data.author ? 'profile' : '—',
      benchmark: 'tone reference',
    };
  }, [data]);

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>master content</h1>
          <p>the canonical YAML files every <span className="mono">jobsmith apply</span> draws from. edits here propagate to every future application.</p>
        </div>
        <div className="actions">
          <button className="btn"><Icon name="doc" size={13} /> validate</button>
          <button className="btn"><Icon name="folder" size={13} /> open in editor</button>
        </div>
      </div>

      <div className="tabs">
        {TABS.map(([id, label]) => (
          <div
            key={id}
            className={`tab ${tab === id ? 'active' : ''}`}
            onClick={() => setTab(id)}
          >
            {label} <span className="tab-count">{tabCounts[id]}</span>
          </div>
        ))}
      </div>

      {(query.isLoading || query.isError) && (
        <QueryStatus
          isLoading={query.isLoading}
          isError={query.isError}
          refetch={() => query.refetch()}
          label="master content"
        />
      )}

      {query.isSuccess && tab === 'work' && <WorkEditor work={data?.work ?? []} />}
      {query.isSuccess && tab === 'skill' && (
        <SectionEditor section="skill" initial={data?.skill ?? []} />
      )}
      {query.isSuccess && tab === 'education' && (
        <SectionEditor section="education" initial={data?.education ?? []} />
      )}
      {query.isSuccess && tab === 'author' && (
        <SectionEditor section="author" initial={data?.author ?? null} />
      )}
      {query.isSuccess && tab === 'benchmark' && (
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <div className="mono-sm">benchmark.md is markdown, not YAML</div>
          <div style={{ fontSize: 12, marginTop: 6 }}>
            edit it with your editor of choice, or use “open in editor” above; a
            web-based markdown editor is part of the 0.8 track.
          </div>
        </div>
      )}
    </div>
  );
}

// ── Generic section editor (MVP — feat-fbc2297e) ────────────────────────────
//
// Edits skill/education/author tabs as a JSON textarea. Why JSON not YAML?
// The API accepts JSON bodies (Pydantic), and adding js-yaml just for human
// editing would be 30+ KB of bundle for a stopgap surface that the 0.8 track
// will replace. Users who prefer YAML can use the Upload .yml button instead.

type SectionInitial =
  | SkillEntry[]
  | EducationEntry[]
  | Author
  | null;

interface SectionEditorProps {
  section: Exclude<MasterSection, 'work'>;
  initial: SectionInitial;
}

function SectionEditor({ section, initial }: SectionEditorProps) {
  const [text, setText] = useState<string>(() => JSON.stringify(initial, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const updateMut = useUpdateMaster(section);
  const uploadMut = useUploadMaster(section);

  function onSave() {
    setParseError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      setParseError(`invalid JSON: ${e instanceof Error ? e.message : 'parse failed'}`);
      return;
    }
    updateMut.reset();
    updateMut.mutate(parsed as never);
  }

  function onUploadClick() {
    fileInputRef.current?.click();
  }

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    uploadMut.reset();
    uploadMut.mutate(file, {
      onSuccess: () => {
        // Re-seed textarea from server-validated value on next refetch — the
        // useMaster query is invalidated by the mutation hook automatically.
      },
    });
    // Reset the input so the same file can be re-uploaded.
    e.target.value = '';
  }

  const apiError = updateMut.error ?? uploadMut.error;
  const apiErrorDetail = formatApiError(apiError);
  const succeeded = updateMut.isSuccess || uploadMut.isSuccess;

  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div className="mono-sm">editor for {section}.yml (json on the wire)</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" type="button" onClick={onUploadClick}
            disabled={uploadMut.isPending}>
            {uploadMut.isPending ? 'uploading…' : 'upload .yml'}
          </button>
          <button className="btn" type="button" onClick={onSave}
            disabled={updateMut.isPending}>
            {updateMut.isPending ? 'saving…' : 'save'}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".yml,.yaml,application/x-yaml,text/yaml,text/plain"
            onChange={onFileChange}
            style={{ display: 'none' }}
          />
        </div>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        style={{
          width: '100%',
          minHeight: 360,
          fontFamily: 'var(--mono, monospace)',
          fontSize: 12,
          padding: 10,
          border: '1px solid var(--border)',
          borderRadius: 4,
          background: 'var(--bg)',
          color: 'var(--fg)',
        }}
      />

      {parseError && (
        <div style={{ marginTop: 8, color: 'var(--danger)' }} className="mono-sm">
          {parseError}
        </div>
      )}
      {apiErrorDetail && (
        <div style={{ marginTop: 8, color: 'var(--danger)' }} className="mono-sm">
          {apiErrorDetail}
        </div>
      )}
      {succeeded && !apiErrorDetail && !parseError && (
        <div style={{ marginTop: 8, color: 'var(--success)' }} className="mono-sm">
          saved · master.yml refreshed
        </div>
      )}
      <div style={{ marginTop: 8, fontSize: 11, color: 'var(--fg-subtle)' }}>
        comments and key order are not preserved across save (yaml.safe_dump).
        the 0.8 track replaces this with ruamel.yaml round-trip or db-canonical
        state.
      </div>
    </div>
  );
}

function formatApiError(err: unknown): string | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
      if (parsed.detail !== undefined) return JSON.stringify(parsed.detail);
    } catch {
      // body wasn't JSON
    }
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

// ── MarkAnchorsView ──────────────────────────────────────────────────────

interface BulletWithDecision extends UiBullet {
  decided: boolean;
  decision: 'anchor' | 'non-anchor' | 'skip' | null;
}

type Decision = 'anchor' | 'non-anchor' | 'skip';

export function MarkAnchorsView() {
  const query = useMaster();
  const work = query.data?.work ?? [];

  // Use the first work entry as the bullet source — matches the design's
  // "Senior Engineer · Recurly Engineering" header.
  const firstEntry = work[0];
  const firstEntryId = firstEntry ? workEntryId(firstEntry, 0) : 'role-0';
  const sourceBullets = useMemo<UiBullet[]>(
    () => firstEntry ? detailsToBullets(firstEntry.details ?? [], firstEntryId) : [],
    [firstEntry, firstEntryId],
  );

  const [bullets, setBullets] = useState<BulletWithDecision[]>([]);
  const [idx, setIdx] = useState<number>(0);

  // Hydrate state once data arrives. Use a structural marker so we re-seed
  // when the source list changes (e.g. on initial fetch) without looping.
  const hydrationKey = `${sourceBullets.length}:${sourceBullets[0]?.id ?? ''}`;
  const [hydratedFor, setHydratedFor] = useState<string>('');
  if (hydrationKey !== hydratedFor && sourceBullets.length > 0) {
    setBullets(
      sourceBullets.map(b => ({
        ...b,
        decided: false,
        decision: b.anchor ? 'anchor' : null,
      })),
    );
    const firstUnanchored = sourceBullets.findIndex(b => !b.anchor);
    setIdx(firstUnanchored === -1 ? 0 : firstUnanchored);
    setHydratedFor(hydrationKey);
  }

  const cur = bullets[idx >= 0 ? idx : 0];
  const done = bullets.length > 0 && bullets.every(b => b.decided);
  const anchorCount = bullets.filter(b => b.decision === 'anchor').length;

  const decide = (decision: Decision) => {
    if (bullets.length === 0) return;
    setBullets(bs =>
      bs.map((b, i) =>
        i === idx ? { ...b, decided: true, decision, anchor: decision === 'anchor' } : b,
      ),
    );
    const next = bullets.findIndex((b, i) => i > idx && !b.decided);
    setIdx(next === -1 ? bullets.length : next);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (bullets.length === 0) return;
    if (e.key === 'a' || e.key === 'A') decide('anchor');
    else if (e.key === 'n' || e.key === 'N') decide('non-anchor');
    else if (e.key === 's' || e.key === 'S') decide('skip');
    else if (e.key === 'ArrowUp') setIdx(i => Math.max(0, i - 1));
    else if (e.key === 'ArrowDown') setIdx(i => Math.min(bullets.length - 1, i + 1));
  };

  const headerTitle = firstEntry
    ? `${firstEntry.title || 'role'} · ${firstEntry.location || '—'}`
    : 'mark anchors';

  return (
    <div className="content" onKeyDown={handleKeyDown} tabIndex={0}>
      <div className="page-head">
        <div>
          <h1>mark anchors</h1>
          <p>walk every bullet in <span className="mono">master/work.yml</span> and tag the ones that must survive every draft.</p>
        </div>
        <div className="actions">
          <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>round-trip via ruamel.yaml — comments preserved</span>
        </div>
      </div>

      {(query.isLoading || query.isError) && (
        <QueryStatus
          isLoading={query.isLoading}
          isError={query.isError}
          refetch={() => query.refetch()}
          label="master content"
        />
      )}

      {query.isSuccess && bullets.length === 0 && (
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <div className="mono-sm">no bullets to review</div>
          <div style={{ fontSize: 12, marginTop: 6 }}>master/work.yml is empty — add a role first.</div>
        </div>
      )}

      {query.isSuccess && bullets.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 16 }}>
          <div className="card">
            <div className="card-h">
              <h3>{headerTitle}</h3>
              <span className="sub">{bullets.filter(b => b.decided).length} / {bullets.length} reviewed</span>
              <div className="right"><Badge kind="accent">{anchorCount} anchors</Badge></div>
            </div>

            {!done ? (
              <div style={{ padding: '24px 28px' }}>
                <div className="mono-sm" style={{ color: 'var(--fg-subtle)', marginBottom: 8 }}>BULLET {idx + 1} OF {bullets.length}</div>
                <div style={{ fontSize: 16, lineHeight: 1.5, marginBottom: 24, fontWeight: 400 }}>
                  {cur && cur.text}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button className="btn primary" onClick={() => decide('anchor')}>
                    <Icon name="flag" size={12} /> anchor <span className="kbd" style={{ marginLeft: 4, background: 'oklch(1 0 0 / 0.15)', borderColor: 'transparent', color: 'inherit' }}>A</span>
                  </button>
                  <button className="btn" onClick={() => decide('non-anchor')}>
                    non-anchor <span className="kbd" style={{ marginLeft: 4 }}>N</span>
                  </button>
                  <button className="btn ghost" onClick={() => decide('skip')}>
                    skip <span className="kbd" style={{ marginLeft: 4 }}>S</span>
                  </button>
                  <div style={{ marginLeft: 'auto', color: 'var(--fg-subtle)', fontSize: 12, alignSelf: 'center' }}>
                    <span className="kbd">↑</span> previous &nbsp; <span className="kbd">↓</span> next
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ padding: '40px 28px', textAlign: 'center' }}>
                <Icon name="check" size={32} style={{ color: 'var(--success)' }} />
                <div style={{ fontSize: 16, marginTop: 10 }}>all bullets reviewed</div>
                <div style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13 }}>
                  {anchorCount} anchors marked · changes ready to write back to work.yml
                </div>
                <button className="btn primary" style={{ marginTop: 18 }}><Icon name="check" size={12} /> save changes</button>
              </div>
            )}

            <div style={{ borderTop: '1px solid var(--border)' }}>
              <div style={{ padding: '10px 16px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>queue</div>
              {bullets.map((b, i) => (
                <div
                  key={b.id}
                  onClick={() => setIdx(i)}
                  style={{
                    padding: '8px 16px',
                    display: 'grid',
                    gridTemplateColumns: '24px 1fr auto',
                    gap: 10,
                    cursor: 'pointer',
                    background: idx === i ? 'var(--bg-sunk)' : 'transparent',
                    borderLeft: idx === i ? '2px solid var(--accent)' : '2px solid transparent',
                    borderBottom: '1px solid var(--border)',
                  }}
                >
                  <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{String(i + 1).padStart(2, '0')}</span>
                  <span style={{ fontSize: 13, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.text}</span>
                  <span>
                    {b.decision === 'anchor'     && <Badge kind="accent">anchor</Badge>}
                    {b.decision === 'non-anchor' && <Badge>non-anchor</Badge>}
                    {b.decision === 'skip'       && <Badge kind="warn">skip</Badge>}
                    {!b.decided                  && <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>—</span>}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="card" style={{ height: 'fit-content' }}>
            <div className="card-h"><h3>shortcuts</h3></div>
            <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
              {([['A', 'mark as anchor'], ['N', 'non-anchor'], ['S', 'skip for now'], ['↑ ↓', 'navigate'], ['⌘ S', 'save & exit']] as [string, string][]).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12.5 }}>
                  <span className="kbd" style={{ minWidth: 36, textAlign: 'center' }}>{k}</span>
                  <span style={{ color: 'var(--fg-muted)' }}>{v}</span>
                </div>
              ))}
            </div>
            <div style={{ borderTop: '1px solid var(--border)', padding: '12px 16px', background: 'var(--bg-sunk)', fontSize: 12, color: 'var(--fg-muted)' }}>
              equivalent to: <span className="mono-sm" style={{ color: 'var(--fg)' }}>jobsmith mark-anchors master/work.yml</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
