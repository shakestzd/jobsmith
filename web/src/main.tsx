import React, { useState, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

import type { ViewName, TweakValues } from './types';
import { useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakToggle } from './tweaks-panel';
import { Sidebar, Topbar } from './app/chrome';
import { getAccessToken, hasStaticToken, login, postApplication } from './api/client';
import { Dashboard } from './app/dashboard';
import { NewApplicationModal } from './app/new';
import { ApplicationDetail } from './app/application';
import { MasterContent, MarkAnchorsView } from './app/master';
import { SiteView, FeedbackView, DoctorView, ConfigView } from './app/views';
import { OnboardWizard } from './app/onboard';
import { PostingsView } from './app/postings';
import { ChatPanel } from './app/chat';
import { ProposalProvider } from './app/proposalContext';

const TWEAK_DEFAULTS: TweakValues = {
  theme: 'light',
  density: 'comfortable',
  showSlugColumn: true,
};

const SIDEBAR_WIDTH = 248;
const CHAT_MIN = 240;
const CHAT_MAX = 640;

function App() {
  const [tweaks, setTweak] = useTweaks<TweakValues>(TWEAK_DEFAULTS);
  const [view, setView] = useState<ViewName>('dashboard');
  const [openSlug, setOpenSlug] = useState<string | null>(null);
  const [showNew, setShowNew] = useState<boolean>(false);
  const [chatOpen, setChatOpen] = useState<boolean>(false);
  const [chatScopeSlug, setChatScopeSlug] = useState<string | null>(null);
  const [chatWidth, setChatWidth] = useState<number>(320);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(true);
  const [authenticated, setAuthenticated] = useState<boolean>(() => hasStaticToken() || Boolean(getAccessToken()));
  const [loginPassword, setLoginPassword] = useState('');
  const [loginError, setLoginError] = useState<string | null>(null);

  // Sync chat scope with the open application slug.
  useEffect(() => {
    setChatScopeSlug(openSlug);
  }, [openSlug]);

  // Apply theme/density to <html>
  useEffect(() => { document.documentElement.dataset.theme = tweaks.theme; }, [tweaks.theme]);
  useEffect(() => { document.documentElement.dataset.density = tweaks.density; }, [tweaks.density]);

  // ⌘N opens new application modal
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        setShowNew(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const cycleTheme = () => {
    const order: TweakValues['theme'][] = ['light', 'dark', 'paper'];
    setTweak('theme', order[(order.indexOf(tweaks.theme) + 1) % order.length]);
  };

  // Chat panel drag-resize — track dragging in a ref to avoid stale closures.
  const chatWidthRef = useRef(chatWidth);
  chatWidthRef.current = chatWidth;

  function handleChatResizeStart(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startW = chatWidthRef.current;
    function onMove(ev: MouseEvent) {
      const delta = startX - ev.clientX;
      setChatWidth(Math.max(CHAT_MIN, Math.min(CHAT_MAX, startW + delta)));
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  // Dynamic grid template: sidebar | main | [chat]
  const gridCols = [
    sidebarOpen ? `${SIDEBAR_WIDTH}px` : '0px',
    '1fr',
    ...(chatOpen ? [`${chatWidth}px`] : []),
  ].join(' ');

  if (!authenticated) {
    return (
      <div className="content" style={{ maxWidth: 420, margin: '80px auto' }}>
        <form
          className="card"
          style={{ padding: 24, display: 'grid', gap: 14 }}
          onSubmit={(e) => {
            e.preventDefault();
            setLoginError(null);
            login(loginPassword)
              .then(() => setAuthenticated(true))
              .catch((err: unknown) => setLoginError(err instanceof Error ? err.message : String(err)));
          }}
        >
          <h2 style={{ margin: 0 }}>jobsmith</h2>
          <input
            className="input"
            type="password"
            value={loginPassword}
            onChange={(e) => setLoginPassword(e.target.value)}
            autoFocus
            placeholder="password"
          />
          {loginError && <div style={{ color: 'var(--danger, #c0392b)', fontSize: 12 }}>{loginError}</div>}
          <button className="btn primary" type="submit">sign in</button>
        </form>
      </div>
    );
  }

  // Build breadcrumbs
  let crumbs: string[];
  if (openSlug) {
    crumbs = ['jobsmith', 'applications', openSlug];
  } else if (view === 'dashboard') { crumbs = ['jobsmith', 'applications'];
  } else if (view === 'running')   { crumbs = ['jobsmith', 'in progress'];
  } else if (view === 'review')    { crumbs = ['jobsmith', 'needs review'];
  } else if (view === 'postings')  { crumbs = ['jobsmith', 'postings'];
  } else if (view === 'master')    { crumbs = ['jobsmith', 'master content'];
  } else if (view === 'anchors')   { crumbs = ['jobsmith', 'mark anchors'];
  } else if (view === 'site')      { crumbs = ['jobsmith', 'listings site'];
  } else if (view === 'feedback')  { crumbs = ['jobsmith', 'feedback'];
  } else if (view === 'doctor')    { crumbs = ['jobsmith', 'doctor'];
  } else if (view === 'config')    { crumbs = ['jobsmith', 'config'];
  } else if (view === 'onboard')   { crumbs = ['jobsmith', 'onboarding'];
  } else { crumbs = ['jobsmith']; }

  // Derive view body
  let body: React.ReactNode;
  if (openSlug) {
    body = <ApplicationDetail slug={openSlug} back={() => setOpenSlug(null)} />;
  } else if (view === 'dashboard') {
    body = <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="all" />;
  } else if (view === 'running') {
    body = <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="running" />;
  } else if (view === 'review') {
    body = <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="review" />;
  } else if (view === 'postings') {
    body = (
      <PostingsView
        onPromoted={(slug: string) => { setOpenSlug(slug); }}
      />
    );
  } else if (view === 'master') { body = <MasterContent />;
  } else if (view === 'anchors') { body = <MarkAnchorsView />;
  } else if (view === 'site')    { body = <SiteView />;
  } else if (view === 'feedback') { body = <FeedbackView />;
  } else if (view === 'doctor')   { body = <DoctorView />;
  } else if (view === 'config')   { body = <ConfigView />;
  } else if (view === 'onboard')  {
    body = (
      <OnboardWizard
        onComplete={() => { setView('master'); }}
        onSkip={() => { setView('dashboard'); }}
      />
    );
  } else {
    body = <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="all" />;
  }

  return (
    <div
      className="shell"
      data-screen-label={openSlug ? `application ${openSlug}` : view}
      data-chat={chatOpen ? 'open' : undefined}
      style={{ gridTemplateColumns: gridCols }}
    >
      <Sidebar
        view={view}
        open={sidebarOpen}
        setView={(v: ViewName) => { setOpenSlug(null); setView(v); }}
        openNew={() => setShowNew(true)}
      />
      <div className="main">
        <Topbar
          crumbs={crumbs}
          onSearch={() => alert('command palette — wire ⌘K to your CLI surface')}
          onTheme={cycleTheme}
          onToggleChat={() => setChatOpen((p) => !p)}
          onToggleSidebar={() => setSidebarOpen((p) => !p)}
          sidebarOpen={sidebarOpen}
          theme={tweaks.theme}
        />
        {body}
      </div>

      {showNew && (
        <NewApplicationModal
          onClose={() => setShowNew(false)}
          onLaunch={(slug: string, url: string, jdText?: string) => {
            setShowNew(false);
            postApplication(url, slug, { jdText })
              .then((created) => setOpenSlug(created.slug))
              .catch(() => setOpenSlug(slug));
          }}
        />
      )}

      {chatOpen && (
        <ChatPanel
          slug={chatScopeSlug}
          open={chatOpen}
          width={chatWidth}
          onClose={() => setChatOpen(false)}
          onScopeChange={setChatScopeSlug}
          onResizeStart={handleChatResizeStart}
        />
      )}

      <TweaksPanel title="Tweaks">
        <TweakSection label="Theme">
          <TweakRadio
            label="mode"
            value={tweaks.theme}
            options={[
              { value: 'light', label: 'light' },
              { value: 'dark', label: 'dark' },
              { value: 'paper', label: 'paper' },
            ]}
            onChange={(v: string) => setTweak('theme', v as TweakValues['theme'])}
          />
        </TweakSection>
        <TweakSection label="Layout">
          <TweakToggle
            label="show slug column"
            value={tweaks.showSlugColumn}
            onChange={(v: boolean) => setTweak('showSlugColumn', v)}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ProposalProvider>
      <App />
    </ProposalProvider>
  </React.StrictMode>,
);
