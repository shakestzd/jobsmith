// BenchmarkEditor.tsx — markdown editor for benchmark.md
//
// Split-pane: textarea on the left, rendered markdown preview on the right.
// No fancy editor library — react-markdown is enough per the project's
// no-bloat default. The save handler (and concurrent-write detection via
// `version`) is wired by slice 14.

import { type ChangeEvent } from 'react';
import ReactMarkdown from 'react-markdown';

interface BenchmarkEditorProps {
  text: string;
  onChange: (next: string) => void;
}

export function BenchmarkEditor({ text, onChange }: BenchmarkEditorProps) {
  return (
    <div className="card" style={{ padding: 0 }}>
      <div className="card-h">
        <h3>benchmark.md</h3>
        <span className="sub">tone reference for every draft</span>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 0,
          height: 'calc(100vh - 280px)',
          minHeight: 400,
        }}
      >
        <textarea
          aria-label="benchmark markdown source"
          value={text}
          onChange={(e: ChangeEvent<HTMLTextAreaElement>) => onChange(e.target.value)}
          style={{
            border: 'none',
            borderRight: '1px solid var(--border)',
            background: 'var(--bg-sunk)',
            padding: '14px 16px',
            fontFamily: 'var(--font-mono)',
            fontSize: 13,
            lineHeight: 1.6,
            resize: 'none',
            outline: 'none',
            color: 'var(--fg)',
          }}
        />
        <div
          aria-label="benchmark markdown preview"
          style={{
            padding: '14px 18px',
            overflow: 'auto',
            fontSize: 13.5,
            lineHeight: 1.55,
            color: 'var(--fg)',
          }}
        >
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}
