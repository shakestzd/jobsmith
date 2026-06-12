// chat.tsx — Global / per-application chat panel (feat-3bd11122 overhaul)
//
// Streams responses from the claude headless backend via SSE.
// History loaded on mount from GET /api/chat/history.
// Sessions persist across page loads; "new session" resets the stored UUID.

import { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import { Icon } from './shared';
import {
  BASE_URL,
  authHeaders,
  chatHistory,
  chatResetSession,
} from '../api/client';
import type { ChatMessage, ChatProposal } from '../api/client';
import { useProposal } from './proposalContext';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ErrorState {
  message: string;   // friendly message shown prominently
  detail: string;    // raw/technical detail shown subtly
  lastUserMsg: string; // for retry
}

// ---------------------------------------------------------------------------
// Markdown components — uses real CSS tokens, no dangerouslySetInnerHTML
// ---------------------------------------------------------------------------

const mdComponents: React.ComponentProps<typeof ReactMarkdown>['components'] = {
  // Code blocks and inline code
  code({ className, children, ...props }) {
    const isBlock = Boolean(className);
    const content = String(children).replace(/\n$/, '');
    if (isBlock) {
      return (
        <pre
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            background: 'var(--bg-code)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: '10px 12px',
            overflowX: 'auto',
            lineHeight: 1.55,
            margin: '6px 0',
            whiteSpace: 'pre',
          }}
        >
          <code style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }} {...props}>
            {content}
          </code>
        </pre>
      );
    }
    return (
      <code
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          background: 'var(--bg-code)',
          border: '1px solid var(--border)',
          padding: '1px 5px',
          borderRadius: 3,
        }}
        {...props}
      >
        {children}
      </code>
    );
  },
  // Headings
  h1({ children }) {
    return (
      <strong style={{ fontSize: 14, display: 'block', marginTop: 8, marginBottom: 3 }}>
        {children}
      </strong>
    );
  },
  h2({ children }) {
    return (
      <strong style={{ fontSize: 13, display: 'block', marginTop: 6, marginBottom: 2 }}>
        {children}
      </strong>
    );
  },
  h3({ children }) {
    return (
      <strong style={{ fontSize: 12.5, display: 'block', marginTop: 5, marginBottom: 2 }}>
        {children}
      </strong>
    );
  },
  // Lists
  ul({ children }) {
    return <ul style={{ paddingLeft: 18, margin: '4px 0', lineHeight: 1.6 }}>{children}</ul>;
  },
  ol({ children }) {
    return <ol style={{ paddingLeft: 18, margin: '4px 0', lineHeight: 1.6 }}>{children}</ol>;
  },
  li({ children }) {
    return <li style={{ marginBottom: 2 }}>{children}</li>;
  },
  // Paragraphs
  p({ children }) {
    return <p style={{ margin: '4px 0', lineHeight: 1.55 }}>{children}</p>;
  },
  // Tables (best-effort, base react-markdown parses simple pipes)
  table({ children }) {
    return (
      <table
        style={{
          borderCollapse: 'collapse',
          fontSize: 12,
          margin: '6px 0',
          width: '100%',
        }}
      >
        {children}
      </table>
    );
  },
  th({ children }) {
    return (
      <th
        style={{
          padding: '3px 8px',
          border: '1px solid var(--border)',
          textAlign: 'left',
          background: 'var(--bg-sunk)',
          fontWeight: 600,
        }}
      >
        {children}
      </th>
    );
  },
  td({ children }) {
    return (
      <td style={{ padding: '3px 8px', border: '1px solid var(--border)' }}>{children}</td>
    );
  },
  // Links
  a({ href, children }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        style={{ color: 'var(--accent)', textDecoration: 'underline' }}
      >
        {children}
      </a>
    );
  },
  // Horizontal rule
  hr() {
    return (
      <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: '8px 0' }} />
    );
  },
  // Blockquote
  blockquote({ children }) {
    return (
      <blockquote
        style={{
          borderLeft: '3px solid var(--accent)',
          marginLeft: 0,
          paddingLeft: 10,
          color: 'var(--fg-muted)',
          fontStyle: 'italic',
        }}
      >
        {children}
      </blockquote>
    );
  },
};

// ---------------------------------------------------------------------------
// Timestamp helper
// ---------------------------------------------------------------------------

function formatTime(ts?: string): string {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffMin < 1440) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ChatPanelProps {
  slug: string | null;
  open: boolean;
  width: number;
  onClose: () => void;
  onScopeChange: (slug: string | null) => void;
  onResizeStart: (e: React.MouseEvent) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ChatPanel({ slug, open, width, onClose, onScopeChange, onResizeStart }: ChatPanelProps) {
  const effectiveSlug = slug ?? '__global__';

  // Proposal state is shared with ReviewTab via context.
  const {
    pendingProposal,
    receiveProposal,
    rejectProposal: handleRejectProposal,
    setOnApplied,
  } = useProposal();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState('');
  const [loadingFirst, setLoadingFirst] = useState(false); // waiting for first chunk
  const [error, setError] = useState<ErrorState | null>(null);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  // Global scope (no slug) cannot apply proposals — apply needs a slug.
  const isGlobal = slug === null;

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Load history when slug changes.
  useEffect(() => {
    if (!open) return;
    setError(null);
    chatHistory(effectiveSlug)
      .then((msgs) => setMessages(msgs))
      .catch(() => setMessages([]));
  }, [effectiveSlug, open]);

  // Auto-scroll to bottom when messages or streaming content changes.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent, loadingFirst]);

  if (!open) return null;

  function handleStop() {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }

  async function sendMessage(userText: string) {
    if (!userText.trim() || streaming) return;
    setError(null);

    // Optimistically add user message.
    const now = new Date().toISOString();
    const userMsg: ChatMessage = { role: 'user', content: userText, created_at: now };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setStreaming(true);
    setLoadingFirst(true);
    setStreamingContent('');

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const resp = await fetch(`${BASE_URL}/api/chat/send`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ message: userText, slug: effectiveSlug }),
        signal: controller.signal,
      });

      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';
      let aborted = false;

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
                proposal?: ChatProposal;
              };
              // Handle backend error event
              if (parsed.error) {
                setError({
                  message: 'The assistant encountered an error.',
                  detail: parsed.error,
                  lastUserMsg: userText,
                });
                setStreamingContent('');
                setStreaming(false);
                setLoadingFirst(false);
                aborted = true;
                break;
              }
              if (parsed.chunk) {
                accumulated += parsed.chunk;
                setLoadingFirst(false);
                setStreamingContent(accumulated);
              }
              if (parsed.proposal) {
                // Delegate to context — fetches OLD content lazily and stores
                // the proposal so ReviewTab can render the full diff.
                receiveProposal(parsed.proposal);
              }
              if (parsed.done) {
                // Finalise: push full assistant message.
                const assistantTs = new Date().toISOString();
                setMessages((prev) => [
                  ...prev,
                  { role: 'assistant', content: accumulated, created_at: assistantTs },
                ]);
                setStreamingContent('');
                setStreaming(false);
                setLoadingFirst(false);
                aborted = true; // use as "done" sentinel to skip the outer catch
                break;
              }
            } catch {
              // Skip non-JSON lines (e.g., SSE comment lines).
            }
          }
        }
        if (aborted) break;
      }

      // If stream ended without a done event but we have content, finalize it
      if (!aborted && accumulated) {
        const assistantTs = new Date().toISOString();
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: accumulated, created_at: assistantTs },
        ]);
        setStreamingContent('');
        setStreaming(false);
        setLoadingFirst(false);
      }
    } catch (err) {
      // AbortError means user clicked Stop — finalize partial content
      if (err instanceof Error && err.name === 'AbortError') {
        const partial = streamingContent;
        if (partial) {
          const ts = new Date().toISOString();
          setMessages((prev) => [
            ...prev,
            { role: 'assistant', content: partial + ' _(stopped)_', created_at: ts },
          ]);
        }
        setStreamingContent('');
        setStreaming(false);
        setLoadingFirst(false);
        return;
      }
      const detail = err instanceof Error ? err.message : 'Unknown error';
      setError({
        message: 'Something went wrong sending your message.',
        detail,
        lastUserMsg: userText,
      });
      setStreamingContent('');
      setStreaming(false);
      setLoadingFirst(false);
    } finally {
      abortRef.current = null;
    }
  }

  async function handleSubmit() {
    await sendMessage(input.trim());
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
      setError(null);
    } catch {
      // Ignore reset errors silently.
    }
  }

  function handleRetry() {
    if (!error) return;
    const msg = error.lastUserMsg;
    setError(null);
    // Remove the last user message if it was already added optimistically
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last?.role === 'user' && last.content === msg) {
        return prev.slice(0, -1);
      }
      return prev;
    });
    void sendMessage(msg);
  }

  function pushAssistantNote(content: string) {
    setMessages((prev) => [
      ...prev,
      { role: 'assistant', content, created_at: new Date().toISOString() },
    ]);
  }

  // Register this chat's pushAssistantNote as the callback for post-apply notes.
  // This runs whenever the chat is mounted/unmounted so ReviewTab can inject a
  // confirmation message into the chat thread after a successful apply.
  useEffect(() => {
    setOnApplied(pushAssistantNote);
    return () => setOnApplied(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setOnApplied]);

  function handleCopy(text: string, idx: number) {
    void navigator.clipboard.writeText(text).then(() => {
      setCopiedIdx(idx);
      setTimeout(() => setCopiedIdx(null), 1500);
    });
  }

  // Regenerate: re-run the user turn that preceded message at index i
  function handleRegenerate(msgIndex: number) {
    if (streaming) return;
    // Find the user message that preceded this assistant message
    let userText = '';
    for (let i = msgIndex - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        userText = messages[i].content;
        break;
      }
    }
    if (!userText) return;
    // Remove from the assistant message onward
    setMessages((prev) => prev.slice(0, msgIndex));
    void sendMessage(userText);
  }

  return (
    <>
      <style>{`
        .chat-panel {
          position: sticky;
          top: 0;
          height: 100vh;
          background: var(--bg-surface, var(--bg));
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
          min-height: 44px;
        }
        .chat-header-title {
          font-size: 13px;
          font-weight: 600;
          color: var(--fg);
          display: flex;
          align-items: center;
          gap: 6px;
          flex-shrink: 0;
        }
        .chat-scope-area {
          flex: 1;
          display: flex;
          align-items: center;
          gap: 6px;
          min-width: 0;
        }
        .chat-scope-label {
          font-size: 11px;
          color: var(--fg-muted);
          white-space: nowrap;
          flex-shrink: 0;
        }
        .chat-scope-badge {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 11px;
          font-family: var(--font-mono);
          padding: 2px 7px;
          border-radius: 4px;
          background: var(--bg-elev);
          color: var(--fg-muted);
          border: 1px solid var(--border);
          cursor: default;
          min-width: 0;
          overflow: hidden;
        }
        .chat-scope-badge .scope-dot {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--accent);
          flex-shrink: 0;
        }
        .chat-scope-badge.global .scope-dot {
          background: var(--fg-muted);
        }
        .chat-scope-badge .scope-text {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          max-width: 120px;
        }
        .chat-scope-clear {
          background: none;
          border: none;
          padding: 0 0 0 2px;
          cursor: pointer;
          color: var(--fg-muted);
          font-size: 11px;
          line-height: 1;
          flex-shrink: 0;
        }
        .chat-scope-clear:hover { color: var(--fg); }
        .chat-messages {
          flex: 1;
          overflow-y: auto;
          padding: 14px;
          display: flex;
          flex-direction: column;
          gap: 10px;
        }
        /* Message row wraps bubble + timestamp + actions */
        .chat-msg-row {
          display: flex;
          flex-direction: column;
          gap: 2px;
          position: relative;
        }
        .chat-msg-row.user { align-items: flex-end; }
        .chat-msg-row.assistant { align-items: flex-start; }
        .chat-bubble {
          max-width: 88%;
          padding: 8px 11px;
          border-radius: var(--radius-lg, 10px);
          font-size: 13px;
          line-height: 1.55;
          word-break: break-word;
        }
        .chat-bubble.user {
          background: var(--accent);
          color: var(--accent-fg, white);
          border-bottom-right-radius: 3px;
          white-space: pre-wrap;
        }
        .chat-bubble.assistant {
          background: var(--bg-elev);
          color: var(--fg);
          border-bottom-left-radius: 3px;
        }
        .chat-bubble.streaming {
          background: var(--bg-elev);
          color: var(--fg);
          border-bottom-left-radius: 3px;
        }
        /* Streaming cursor */
        .streaming-cursor {
          display: inline-block;
          width: 7px;
          height: 13px;
          background: var(--accent);
          margin-left: 2px;
          vertical-align: middle;
          animation: chat-blink 1s step-end infinite;
        }
        @keyframes chat-blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
        /* Typing dots for loading first chunk */
        .chat-typing-dots {
          display: flex;
          align-items: center;
          gap: 4px;
          padding: 10px 14px;
          background: var(--bg-elev);
          border-radius: var(--radius-lg, 10px);
          border-bottom-left-radius: 3px;
          align-self: flex-start;
        }
        .chat-typing-dots span {
          display: inline-block;
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--fg-muted);
        }
        @media (prefers-reduced-motion: no-preference) {
          .chat-typing-dots span:nth-child(1) {
            animation: chat-dot-bounce 1.2s ease-in-out infinite 0ms;
          }
          .chat-typing-dots span:nth-child(2) {
            animation: chat-dot-bounce 1.2s ease-in-out infinite 200ms;
          }
          .chat-typing-dots span:nth-child(3) {
            animation: chat-dot-bounce 1.2s ease-in-out infinite 400ms;
          }
        }
        @keyframes chat-dot-bounce {
          0%, 80%, 100% { transform: translateY(0); opacity: 0.5; }
          40% { transform: translateY(-4px); opacity: 1; }
        }
        /* Message meta: timestamp + hover actions */
        .chat-msg-meta {
          display: flex;
          align-items: center;
          gap: 6px;
          opacity: 0;
          transition: opacity 120ms;
          padding: 0 2px;
        }
        .chat-msg-row:hover .chat-msg-meta { opacity: 1; }
        .chat-msg-row.user .chat-msg-meta { flex-direction: row-reverse; }
        .chat-ts {
          font-size: 10px;
          color: var(--fg-subtle);
          font-family: var(--font-mono);
          user-select: none;
        }
        .chat-action-btn {
          background: none;
          border: 1px solid var(--border);
          border-radius: var(--radius-sm, 4px);
          padding: 2px 6px;
          font-size: 10px;
          color: var(--fg-muted);
          cursor: pointer;
          font-family: var(--font-sans);
          line-height: 1.4;
          transition: background 80ms, color 80ms;
        }
        .chat-action-btn:hover {
          background: var(--bg-sunk);
          color: var(--fg);
          border-color: var(--border-strong);
        }
        /* Error chip */
        .chat-error-row {
          display: flex;
          align-items: flex-start;
          gap: 8px;
          padding: 9px 12px;
          background: var(--danger-soft);
          border: 1px solid var(--danger);
          border-radius: var(--radius, 6px);
          font-size: 12.5px;
          color: var(--danger);
          position: relative;
        }
        .chat-error-icon { flex-shrink: 0; font-size: 14px; line-height: 1.4; }
        .chat-error-body { flex: 1; display: flex; flex-direction: column; gap: 3px; }
        .chat-error-msg { font-weight: 500; }
        .chat-error-detail {
          font-size: 11px;
          color: var(--fg-muted);
          font-family: var(--font-mono);
          word-break: break-all;
        }
        .chat-error-actions { display: flex; gap: 6px; margin-top: 4px; }
        .chat-error-retry {
          background: var(--danger);
          color: white;
          border: none;
          border-radius: var(--radius-sm, 4px);
          padding: 3px 10px;
          font-size: 11.5px;
          cursor: pointer;
          font-weight: 500;
          font-family: var(--font-sans);
        }
        .chat-error-retry:hover { filter: brightness(1.1); }
        .chat-error-dismiss {
          background: none;
          border: 1px solid var(--danger);
          color: var(--danger);
          border-radius: var(--radius-sm, 4px);
          padding: 3px 10px;
          font-size: 11.5px;
          cursor: pointer;
          font-family: var(--font-sans);
        }
        .chat-error-dismiss:hover { background: var(--danger-soft); }
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
          border-radius: var(--radius, 6px);
          padding: 7px 10px;
          font-size: 13px;
          font-family: var(--font-sans);
          background: var(--bg);
          color: var(--fg);
          min-height: 36px;
          max-height: 120px;
          line-height: 1.4;
          outline: none;
        }
        .chat-textarea:focus { border-color: var(--accent); }
        .chat-textarea::placeholder { color: var(--fg-subtle); }
        .chat-bottom-bar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }
        .chat-new-session {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          background: none;
          border: 1px solid var(--border);
          border-radius: var(--radius, 6px);
          padding: 4px 10px;
          font-size: 12px;
          color: var(--fg-muted);
          cursor: pointer;
          font-family: var(--font-sans);
          font-weight: 500;
          transition: background 100ms, color 100ms, border-color 100ms;
        }
        .chat-new-session:hover {
          background: var(--bg-sunk);
          color: var(--fg);
          border-color: var(--border-strong);
        }
        .chat-new-session:disabled { opacity: 0.45; cursor: not-allowed; }
        .chat-stop-btn {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          background: var(--danger-soft);
          border: 1px solid var(--danger);
          border-radius: var(--radius, 6px);
          padding: 4px 10px;
          font-size: 12px;
          color: var(--danger);
          cursor: pointer;
          font-family: var(--font-sans);
          font-weight: 500;
          transition: background 100ms;
        }
        .chat-stop-btn:hover { background: var(--danger); color: white; }
        .chat-empty {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          color: var(--fg-muted);
          font-size: 12px;
          text-align: center;
          padding: 20px;
        }
        /* Compact proposal pointer chip (full diff lives in Review tab) */
        .chat-proposal-chip {
          align-self: stretch;
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 9px 12px;
          background: var(--bg-elev);
          border: 1px solid var(--accent);
          border-radius: var(--radius-lg, 10px);
          font-size: 12px;
          color: var(--fg);
        }
        .chat-proposal-chip-icon { flex-shrink: 0; font-size: 14px; line-height: 1; }
        .chat-proposal-chip-text { flex: 1; line-height: 1.4; color: var(--fg-muted); }
        .chat-proposal-chip-text strong { color: var(--fg); font-weight: 600; }
        .chat-proposal-chip-view {
          flex-shrink: 0;
          background: none;
          border: 1px solid var(--border);
          border-radius: var(--radius-sm, 4px);
          padding: 3px 9px;
          font-size: 11px;
          color: var(--fg-muted);
          cursor: pointer;
          font-family: var(--font-sans);
          transition: background 80ms, color 80ms;
        }
        .chat-proposal-chip-view:hover { background: var(--bg-sunk); color: var(--fg); border-color: var(--border-strong); }
        /* Scope header bar */
        .chat-scope-bar {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 6px 14px;
          background: var(--bg-elev);
          border-bottom: 1px solid var(--border);
          font-size: 12px;
          color: var(--fg-muted);
          flex-shrink: 0;
        }
        .chat-scope-bar .scope-context {
          flex: 1;
          display: flex;
          align-items: center;
          gap: 5px;
          font-style: italic;
          overflow: hidden;
        }
        .chat-scope-bar .scope-name {
          font-family: var(--font-mono);
          font-size: 11.5px;
          color: var(--fg);
          font-style: normal;
          font-weight: 500;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
      `}</style>

      <div className="chat-panel" style={{ width }}>
        {/* Drag resize handle on left edge */}
        <div className="chat-resize-handle" onMouseDown={onResizeStart} aria-hidden="true" />

        {/* Header */}
        <div className="chat-header">
          <span className="chat-header-title">
            <Icon name="msg" size={14} />
            chat
          </span>

          <div className="chat-scope-area">
            {slug !== null ? (
              <span className="chat-scope-badge" title={`Scoped to application: ${slug}`}>
                <span className="scope-dot" />
                <span className="scope-text">{slug}</span>
                <button
                  className="chat-scope-clear"
                  onClick={() => onScopeChange(null)}
                  title="Switch to global chat"
                  aria-label="Switch to global chat"
                >
                  ✕
                </button>
              </span>
            ) : (
              <span className="chat-scope-badge global" title="Global chat — not scoped to an application">
                <span className="scope-dot" />
                <span className="scope-text">global</span>
              </span>
            )}
          </div>

          <button
            className="btn ghost sm"
            onClick={onClose}
            title="Close chat"
            aria-label="Close chat"
            style={{ marginLeft: 2 }}
          >
            <Icon name="x" size={13} />
          </button>
        </div>

        {/* Scope context bar — shows what you're chatting about prominently */}
        <div className="chat-scope-bar">
          <span className="scope-context">
            {isGlobal ? (
              'Chatting globally — ask anything about your job search'
            ) : (
              <>
                Chatting about:&nbsp;
                <span className="scope-name">{slug}</span>
              </>
            )}
          </span>
        </div>

        {/* Messages */}
        <div className="chat-messages">
          {messages.length === 0 && !streaming && !streamingContent && !error && (
            <div className="chat-empty">
              {isGlobal
                ? 'Ask anything about your job search'
                : `Ask anything about your application for ${slug}`}
            </div>
          )}

          {messages.map((msg, i) =>
            msg.role === 'user' ? (
              <div key={i} className="chat-msg-row user">
                <div className="chat-bubble user">{msg.content}</div>
                <div className="chat-msg-meta">
                  {msg.created_at && (
                    <span className="chat-ts">{formatTime(msg.created_at)}</span>
                  )}
                  <button
                    className="chat-action-btn"
                    onClick={() => handleCopy(msg.content, i)}
                    title="Copy message"
                    aria-label="Copy message"
                  >
                    {copiedIdx === i ? 'copied' : 'copy'}
                  </button>
                </div>
              </div>
            ) : (
              <div key={i} className="chat-msg-row assistant">
                <div className="chat-bubble assistant">
                  <ReactMarkdown components={mdComponents}>
                    {msg.content}
                  </ReactMarkdown>
                </div>
                <div className="chat-msg-meta">
                  {msg.created_at && (
                    <span className="chat-ts">{formatTime(msg.created_at)}</span>
                  )}
                  <button
                    className="chat-action-btn"
                    onClick={() => handleCopy(msg.content, i)}
                    title="Copy response"
                    aria-label="Copy response"
                  >
                    {copiedIdx === i ? 'copied' : 'copy'}
                  </button>
                  <button
                    className="chat-action-btn"
                    onClick={() => handleRegenerate(i)}
                    disabled={streaming}
                    title="Regenerate this response"
                    aria-label="Regenerate this response"
                  >
                    regenerate
                  </button>
                </div>
              </div>
            )
          )}

          {/* Compact proposal pointer chip — full diff + Apply/Reject lives in the Review tab */}
          {pendingProposal && (
            <div className="chat-proposal-chip" role="status" aria-label="Proposal pending review">
              <span className="chat-proposal-chip-icon">✍️</span>
              <span className="chat-proposal-chip-text">
                Proposed a {pendingProposal.proposal.asset === 'resume'
                  ? `resume edit (${pendingProposal.proposal.target_section ?? 'section'})`
                  : 'cover-letter revision'} — review &amp; apply in the{' '}
                <strong>Review tab</strong>.
              </span>
              {!isGlobal && (
                <button
                  className="chat-proposal-chip-view"
                  aria-label="View cover letter proposal in Review tab"
                  onClick={handleRejectProposal}
                  title="Dismiss this pointer (the proposal is in the Review tab)"
                >
                  dismiss
                </button>
              )}
            </div>
          )}

          {/* Typing / loading indicator — before first chunk arrives */}
          {loadingFirst && (
            <div className="chat-typing-dots" role="status" aria-label="Assistant is typing">
              <span />
              <span />
              <span />
            </div>
          )}

          {/* Streaming preview with cursor */}
          {streaming && streamingContent && (
            <div className="chat-msg-row assistant">
              <div className="chat-bubble streaming">
                <ReactMarkdown components={mdComponents}>
                  {streamingContent}
                </ReactMarkdown>
                <span className="streaming-cursor" aria-hidden="true" />
              </div>
            </div>
          )}

          {/* Error chip */}
          {error && (
            <div className="chat-error-row" role="alert">
              <span className="chat-error-icon" aria-hidden="true">⚠</span>
              <div className="chat-error-body">
                <span className="chat-error-msg">{error.message}</span>
                {error.detail && (
                  <span className="chat-error-detail">{error.detail}</span>
                )}
                <div className="chat-error-actions">
                  <button
                    className="chat-error-retry"
                    onClick={handleRetry}
                    aria-label="Retry sending the last message"
                  >
                    Retry
                  </button>
                  <button
                    className="chat-error-dismiss"
                    onClick={() => setError(null)}
                    aria-label="Dismiss error"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
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
              placeholder="Message… (Enter to send, Shift+Enter for newline)"
              rows={1}
              disabled={streaming}
              aria-label="Chat message input"
            />
            <button
              className="btn primary sm"
              onClick={() => void handleSubmit()}
              disabled={streaming || !input.trim()}
              title="Send (Enter)"
              aria-label="Send message"
            >
              ↵
            </button>
          </div>
          <div className="chat-bottom-bar">
            <button
              className="chat-new-session"
              onClick={() => void handleNewSession()}
              disabled={streaming}
              title="Start a new chat session"
              aria-label="Start a new chat session"
            >
              + new session
            </button>
            {streaming && (
              <button
                className="chat-stop-btn"
                onClick={handleStop}
                title="Stop generating"
                aria-label="Stop generating response"
              >
                ■ stop
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
