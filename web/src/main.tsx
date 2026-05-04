import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import './styles.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Pipeline data changes through user actions, not background events
      // we can't observe — keep it cached briefly and let mutations
      // invalidate explicitly.
      staleTime: 5_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

import type { ViewName, TweakValues } from './types';
import { useTweaks, TweaksPanel, TweakSection, TweakRadio, TweakToggle } from './tweaks-panel';
import { Sidebar, Topbar } from './app/chrome';
import { Dashboard } from './app/dashboard';
import { NewApplicationModal } from './app/new';
import { ApplicationDetail } from './app/application';
import { MasterContent, MarkAnchorsView } from './app/master';
import { SiteView, FeedbackView, DoctorView, ConfigView } from './app/views';

const TWEAK_DEFAULTS: TweakValues = {
  theme: 'light',
  density: 'comfortable',
  showSlugColumn: true,
};

function App() {
  const [tweaks, setTweak] = useTweaks<TweakValues>(TWEAK_DEFAULTS);
  const [view, setView] = useState<ViewName>('dashboard');
  const [openSlug, setOpenSlug] = useState<string | null>(null);
  const [showNew, setShowNew] = useState<boolean>(false);

  // Apply theme attribute to <html>
  useEffect(() => {
    document.documentElement.dataset.theme = tweaks.theme;
  }, [tweaks.theme]);

  // Apply density attribute to <html>
  useEffect(() => {
    document.documentElement.dataset.density = tweaks.density;
  }, [tweaks.density]);

  // ⌘N (or Ctrl+N) opens new application modal
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

  // Build breadcrumbs
  let crumbs: string[];
  if (openSlug) {
    crumbs = ['jobsmith', 'applications', openSlug];
  } else if (view === 'dashboard') {
    crumbs = ['jobsmith', 'applications'];
  } else if (view === 'running') {
    crumbs = ['jobsmith', 'in progress'];
  } else if (view === 'review') {
    crumbs = ['jobsmith', 'needs review'];
  } else if (view === 'master') {
    crumbs = ['jobsmith', 'master content'];
  } else if (view === 'anchors') {
    crumbs = ['jobsmith', 'mark anchors'];
  } else if (view === 'site') {
    crumbs = ['jobsmith', 'listings site'];
  } else if (view === 'feedback') {
    crumbs = ['jobsmith', 'feedback'];
  } else if (view === 'doctor') {
    crumbs = ['jobsmith', 'doctor'];
  } else if (view === 'config') {
    crumbs = ['jobsmith', 'config'];
  } else {
    crumbs = ['jobsmith'];
  }

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
  } else if (view === 'master') {
    body = <MasterContent />;
  } else if (view === 'anchors') {
    body = <MarkAnchorsView />;
  } else if (view === 'site') {
    body = <SiteView />;
  } else if (view === 'feedback') {
    body = <FeedbackView />;
  } else if (view === 'doctor') {
    body = <DoctorView />;
  } else if (view === 'config') {
    body = <ConfigView />;
  } else {
    body = <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="all" />;
  }

  return (
    <div className="shell" data-screen-label={openSlug ? `application ${openSlug}` : view}>
      <Sidebar
        view={view}
        setView={(v: ViewName) => {
          setOpenSlug(null);
          setView(v);
        }}
        openNew={() => setShowNew(true)}
      />
      <div className="main">
        <Topbar
          crumbs={crumbs}
          onSearch={() => alert('command palette — wire ⌘K to your CLI surface')}
          onTheme={cycleTheme}
          theme={tweaks.theme}
        />
        {body}
      </div>

      {showNew && (
        <NewApplicationModal
          onClose={() => setShowNew(false)}
          onLaunch={(slug: string) => {
            setShowNew(false);
            setOpenSlug(slug);
          }}
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
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
