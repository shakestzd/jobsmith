// chat.tsx — Global / per-application chat panel (feat-7b2b70ef)
//
// A fixed right-side drawer that streams responses from the claude headless
// backend via SSE. History is loaded on mount from GET /api/chat/history.
// Sessions persist across page loads; "new session" resets the stored UUID.

import { useState, useEffect, useRef } from 'react';
import { Icon } from './shared';
import { BASE_URL, authHeaders, chatHistory, chatResetSession } from '../api/client';
import type { ChatMessage } from '../api/client';

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function applyInline(s: string): string {
  return s
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\*([^*]+)\*/g, '<i>$1</i>')
    .replace(/`([^`]+)`/g, '<code style="font-family:var(--font-mono);font-size:11px;background:var(--bg-elevated);padding:1px 4px;border-radius:3px">$1</code>');
}

function renderMarkdown(text: string): string {
  const lines = text.split('\n');
  const out: string[] = [];
  for (const raw of lines) {
    const escaped = escapeHtml(raw);
    // Headings
    if (/^#{1,3}\s/.test(raw)) {
      const level = raw.match(/^(#+)/)?.[1].length ?? 2;
      const content = applyInline(escaped.replace(/^#{1,3}\s+/, ''));
      out.push(`<b style="font-size:${level === 1 ? 14 : 13}px;display:block;margin-top:6px;margin-bottom:2px">${content}</b>`);
      continue;
    }
    // Table separator row — skip
    if (/^\|[-| :]+\|$/.test(raw)) continue;
    // Table rows — apply inline formatting inside each cell
    if (raw.startsWith('|') && raw.endsWith('|')) {
      const cells = escaped.slice(1, -1).split('|')
        .map(c => `<td style="padding:2px 6px;border:1px solid var(--border)">${applyInline(c.trim())}</td>`)
        .join('');
      out.push(`<tr>${cells}</tr>`);
      continue;
    }
    // Horizontal rule
    if (/^---+$/.test(raw)) { out.push('<hr style="border:none;border-top:1px solid var(--border);margin:6px 0">'); continue; }
    // Regular line with inline formatting
    const line = applyInline(escaped);
    out.push(line ? `<div>${line}</div>` : '<div style="height:6px"></div>');
  }
  // Wrap consecutive <tr> blocks in a table
  const joined = out.join('');
  return joined.replace(/(<tr>.*?<\/tr>)+/gs, (m) =>
    `<table style="border-collapse:collapse;font-size:12px;margin:4px 0">${m}</table>`
  );
}

export interface ChatPanelProps {
  slug: string | null;
  open: boolean;
  width: number;
  onClose: () => void;
  onScopeChange: (slug: string | null) => void;
  onResizeStart: (e: React.MouseEvent) => void;
}

export function ChatPanel({ slug, open, width, onClose, onScopeChange, onResizeStart }: ChatPanelProps) {
  const effectiveSlug = slug ?? '__global__';

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState('');

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Load history when slug changes.
  useEffect(() => {
    if (!open) return;
    chatHistory(effectiveSlug)
      .then((msgs) => setMessages(msgs))
      .catch(() => setMessages([]));
  }, [effectiveSlug, open]);

  // Auto-scroll to bottom when messages or streaming content changes.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent]);

  if (!open) return null;

  async function handleSubmit() {
    const trimmed = input.trim();
    if (!trimmed || streaming) return;

    // Optimistically add user message.
    const userMsg: ChatMessage = { role: 'user', content: trimmed };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setStreaming(true);
    setStreamingContent('');

    try {
      const resp = await fetch(`${BASE_URL}/api/chat/send`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ message: trimmed, slug: effectiveSlug }),
      });

      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const raw = line.slice(6).trim();
            try {
              const parsed = JSON.parse(raw) as {
                chunk?: string;
                done?: boolean;
                session_id?: string;
                error?: string;
              };
              if (parsed.chunk) {
                accumulated += parsed.chunk;
                setStreamingContent(accumulated);
              }
              if (parsed.done) {
                // Finalise: push full assistant message.
                setMessages((prev) => [
                  ...prev,
                  { role: 'assistant', content: accumulated },
                ]);
                setStreamingContent('');
                setStreaming(false);
              }
            } catch {
              // Skip non-JSON lines (e.g., SSE comment lines).
            }
          }
        }
      }
    } catch (err) {
      const errorMsg =
        err instanceof Error ? err.message : 'Unknown error';
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `Error: ${errorMsg}` },
      ]);
      setStreamingContent('');
      setStreaming(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  }

  async function handleNewSession() {
    try {
      await chatResetSession(effectiveSlug);
      setMessages([]);
    } catch {
      // Ignore reset errors silently.
    }
  }

  return (
    <>
      <style>{`
        .chat-panel {
          position: sticky;
          top: 0;
          height: 100vh;
          background: var(--bg-surface);
          border-left: 1px solid var(--border);
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        .chat-resize-handle {
          position: absolute;
          left: 0;
          top: 0;
          bottom: 0;
          width: 5px;
          cursor: col-resize;
          z-index: 10;
          background: transparent;
          transition: background 120ms;
        }
        .chat-resize-handle:hover {
          background: var(--accent);
          opacity: 0.35;
        }
        .chat-header {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 10px 14px;
          border-bottom: 1px solid var(--border);
          flex-shrink: 0;
        }
        .chat-header-title {
          font-size: 13px;
          font-weight: 600;
          color: var(--text);
          flex: 1;
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .chat-scope-badge {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 11px;
          font-family: var(--font-mono);
          padding: 2px 7px;
          border-radius: 4px;
          background: var(--bg-elevated);
          color: var(--text-muted);
          border: 1px solid var(--border);
          cursor: default;
        }
        .chat-scope-badge .scope-dot {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--accent);
          flex-shrink: 0;
        }
        .chat-scope-badge.global .scope-dot {
          background: var(--text-muted);
        }
        .chat-scope-clear {
          background: none;
          border: none;
          padding: 0 0 0 2px;
          cursor: pointer;
          color: var(--text-muted);
          font-size: 11px;
          line-height: 1;
        }
        .chat-scope-clear:hover {
          color: var(--text);
        }
        .chat-messages {
          flex: 1;
          overflow-y: auto;
          padding: 14px;
          display: flex;
          flex-direction: column;
          gap: 10px;
        }
        .chat-bubble {
          max-width: 88%;
          padding: 8px 11px;
          border-radius: 10px;
          font-size: 13px;
          line-height: 1.55;
          white-space: pre-wrap;
          word-break: break-word;
        }
        .chat-bubble.user {
          align-self: flex-end;
          background: var(--accent);
          color: white;
          border-bottom-right-radius: 3px;
        }
        .chat-bubble.assistant {
          align-self: flex-start;
          background: var(--bg-elevated);
          color: var(--text);
          border-bottom-left-radius: 3px;
        }
        .chat-bubble.streaming {
          align-self: flex-start;
          background: var(--bg-elevated);
          color: var(--text);
          border-bottom-left-radius: 3px;
        }
        .streaming-cursor {
          display: inline-block;
          width: 7px;
          height: 13px;
          background: var(--accent);
          margin-left: 2px;
          vertical-align: middle;
          animation: blink 1s step-end infinite;
        }
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
        .chat-footer {
          padding: 10px 12px;
          border-top: 1px solid var(--border);
          display: flex;
          flex-direction: column;
          gap: 6px;
          flex-shrink: 0;
        }
        .chat-input-row {
          display: flex;
          gap: 6px;
          align-items: flex-end;
        }
        .chat-textarea {
          flex: 1;
          resize: none;
          border: 1px solid var(--border);
          border-radius: 6px;
          padding: 7px 10px;
          font-size: 13px;
          font-family: var(--font-sans);
          background: var(--bg);
          color: var(--text);
          min-height: 36px;
          max-height: 120px;
          line-height: 1.4;
          outline: none;
        }
        .chat-textarea:focus {
          border-color: var(--accent);
        }
        .chat-actions {
          display: flex;
          align-items: center;
          justify-content: flex-start;
        }
        .chat-empty {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          color: var(--text-muted);
          font-size: 12px;
          text-align: center;
          padding: 20px;
        }
      `}</style>

      <div className="chat-panel" style={{ width }}>
        {/* Drag resize handle on left edge */}
        <div className="chat-resize-handle" onMouseDown={onResizeStart} />
        {/* Header */}
        <div className="chat-header">
          <span className="chat-header-title">
            <Icon name="msg" size={14} />
            chat
          </span>

          {/* Scope badge */}
          {slug !== null ? (
            <span className="chat-scope-badge">
              <span className="scope-dot" />
              {slug}
              <button
                className="chat-scope-clear"
                onClick={() => onScopeChange(null)}
                title="switch to global chat"
              >
                ✕
              </button>
            </span>
          ) : (
            <span className="chat-scope-badge global">
              <span className="scope-dot" />
              global
            </span>
          )}

          <button
            className="btn ghost sm"
            onClick={onClose}
            title="close chat"
            style={{ marginLeft: 2 }}
          >
            <Icon name="x" size={13} />
          </button>
        </div>

        {/* Messages */}
        <div className="chat-messages">
          {messages.length === 0 && !streaming && !streamingContent && (
            <div className="chat-empty">
              ask anything about your applications
            </div>
          )}

          {messages.map((msg, i) =>
            msg.role === 'user' ? (
              <div key={i} className="chat-bubble user">{msg.content}</div>
            ) : (
              <div
                key={i}
                className="chat-bubble assistant"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
              />
            )
          )}

          {streaming && streamingContent && (
            <div
              className="chat-bubble streaming"
              dangerouslySetInnerHTML={{
                __html: renderMarkdown(streamingContent) + '<span class="streaming-cursor"></span>',
              }}
            />
          )}

          {streaming && !streamingContent && (
            <div className="chat-bubble streaming">
              <span className="streaming-cursor" />
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Footer */}
        <div className="chat-footer">
          <div className="chat-input-row">
            <textarea
              ref={textareaRef}
              className="chat-textarea"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="message… (Enter to send, Shift+Enter for newline)"
              rows={1}
              disabled={streaming}
            />
            <button
              className="btn primary sm"
              onClick={() => void handleSubmit()}
              disabled={streaming || !input.trim()}
              title="send (Enter)"
            >
              ↵
            </button>
          </div>
          <div className="chat-actions">
            <button
              className="btn ghost sm"
              onClick={() => void handleNewSession()}
              disabled={streaming}
              title="start a new session"
              style={{ fontSize: 11, color: 'var(--text-muted)' }}
            >
              new session
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
