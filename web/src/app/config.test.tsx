// config.test.tsx — unit tests for ConfigView
//
// Assertions:
//   GET /api/config hydrates form inputs
//   Validate button calls POST /api/config/validate and renders errors
//   Save button calls PUT /api/config and shows success/422 feedback

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ConfigView } from './views';

// ── Shared mock config returned by GET /api/config ───────────────────────

const MOCK_CONFIG = {
  master: {
    work_yml: 'assets/content/work.yml',
    skill_yml: 'assets/content/skill.yml',
    education_yml: 'assets/content/education.yml',
    author_yml: 'assets/content/author.yml',
    publication_yml: null,
    award_yml: null,
    projects_yml: null,
  },
  output: {
    applications_dir: 'private/applications',
    job_search_db: 'private/job_search.db',
    jobsmith_db: 'private/jobsmith.db',
    review_db_dir: 'private/.review',
  },
  user: {
    name: 'Jordan Smith',
    email: 'jordan@example.com',
    phone: '+1-555-0100',
    location: 'San Francisco, CA',
    github: 'github.com/jordan',
    linkedin: 'linkedin.com/in/jordan',
  },
  voice: {},
  anchor_thresholds: {},
  cover_letter: {},
  resume: {},
  fit_scorer: {},
  portfolio: {},
  benchmarks: {},
  llm: {
    provider: 'claude_cli' as const,
    model: null,
    base_url: null,
    api_key: null,
    budget_usd: 1.0,
    timeout_s: 300,
  },
};

// ── Mock the API client module ────────────────────────────────────────────

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  // OfflineModeToggle is now mounted inside ConfigView. These mocks make it
  // hide silently (404-like rejection → phase='hidden' → renders null) so
  // it doesn't interfere with the config form tests.
  getLlmStatus: vi.fn().mockRejectedValue(new Error('not desktop')),
  enableOfflineMode: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));

import { apiGet, apiPost, apiPut } from '../api/client';

const mockApiGet = vi.mocked(apiGet);
const mockApiPost = vi.mocked(apiPost);
const mockApiPut = vi.mocked(apiPut);

beforeEach(() => {
  vi.clearAllMocks();
  // Default: GET resolves with mock config
  mockApiGet.mockResolvedValue(MOCK_CONFIG);
});

// ── Tests ─────────────────────────────────────────────────────────────────

describe('ConfigView', () => {
  it('renders a load-error state when GET /api/config fails (does not show form)', async () => {
    mockApiGet.mockRejectedValue(new Error('boom'));
    render(<ConfigView />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load config: boom/)).toBeInTheDocument();
    });
    // The form must not render — saving from blank local state would PUT a partial config.
    expect(screen.queryByDisplayValue('assets/content/work.yml')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
  });

  it('renders a validate-failure message when /api/config/validate throws', async () => {
    render(<ConfigView />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('assets/content/work.yml')).toBeInTheDocument(),
    );
    mockApiPost.mockRejectedValue(new Error('network down'));

    fireEvent.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(screen.getByText(/validate request failed/)).toBeInTheDocument();
    });
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });

  it('shows a loading state while GET is in flight', () => {
    // GET never resolves in this test — stays pending
    mockApiGet.mockReturnValue(new Promise(() => {}));
    render(<ConfigView />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('GET /api/config hydrates master file inputs', async () => {
    render(<ConfigView />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('assets/content/work.yml')).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue('assets/content/skill.yml')).toBeInTheDocument();
    expect(screen.getByDisplayValue('assets/content/education.yml')).toBeInTheDocument();
    expect(screen.getByDisplayValue('assets/content/author.yml')).toBeInTheDocument();
  });

  it('GET /api/config hydrates workspace (output) inputs', async () => {
    render(<ConfigView />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('private/applications')).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue('private/jobsmith.db')).toBeInTheDocument();
  });

  it('validate button calls POST /api/config/validate', async () => {
    mockApiPost.mockResolvedValue({ ok: true, errors: [] });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    expect(mockApiPost).toHaveBeenCalledOnce();
    expect(mockApiPost).toHaveBeenCalledWith('/api/config/validate', expect.any(Object));
  });

  it('shows validation success when POST returns ok:true', async () => {
    mockApiPost.mockResolvedValue({ ok: true, errors: [] });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    await waitFor(() => expect(screen.getByText(/config is valid/i)).toBeInTheDocument());
  });

  it('renders inline errors when POST returns ok:false', async () => {
    mockApiPost.mockResolvedValue({
      ok: false,
      errors: [{ field: 'user.email', message: 'value is not a valid email address' }],
    });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    await waitFor(() =>
      expect(screen.getByText(/value is not a valid email address/i)).toBeInTheDocument(),
    );
  });

  it('save button calls PUT /api/config', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    expect(mockApiPut).toHaveBeenCalledOnce();
    expect(mockApiPut).toHaveBeenCalledWith('/api/config', expect.any(Object));
  });

  it('shows success message after PUT /api/config resolves', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    await waitFor(() => expect(screen.getByText('saved')).toBeInTheDocument());
  });

  it('shows 422 errors inline when PUT returns 422', async () => {
    const { JobsmithApiError } = await import('../api/client');
    const err = new JobsmithApiError(
      JSON.stringify([{ field: 'master.work_yml', message: 'path not found' }]),
      422,
    );
    mockApiPut.mockRejectedValue(err);

    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    await waitFor(() => {
      // The error panel header is always a single text node — safe to match exactly.
      expect(screen.getByText('validation errors')).toBeInTheDocument();
      // The field name is rendered in its own <span> — also a single text node.
      expect(screen.getByText('master.work_yml')).toBeInTheDocument();
    });
  });
});

// ── LLM provider section ──────────────────────────────────────────────────

describe('ConfigView — LLM provider section', () => {
  it('renders the provider selector with all four options', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    const select = screen.getByRole('combobox', { name: /provider/i });
    expect(select).toBeInTheDocument();

    const options = Array.from(select.querySelectorAll('option')).map(o => o.value);
    expect(options).toContain('claude_cli');
    expect(options).toContain('antigravity_cli');
    expect(options).toContain('codex_cli');
    expect(options).toContain('openai_compatible');
  });

  it('hydrates provider from GET response (claude_cli default)', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    const select = screen.getByRole('combobox', { name: /provider/i });
    expect((select as HTMLSelectElement).value).toBe('claude_cli');
  });

  it('base_url and model inputs are hidden when provider is claude_cli', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    // base_url and model inputs must not be in the DOM when provider !== openai_compatible
    expect(screen.queryByLabelText(/base url/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/model/i)).not.toBeInTheDocument();
  });

  it('base_url and model inputs appear when provider switches to openai_compatible', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    const select = screen.getByRole('combobox', { name: /provider/i });
    await act(async () => {
      fireEvent.change(select, { target: { value: 'openai_compatible' } });
    });

    expect(screen.getByLabelText(/base url/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/model/i)).toBeInTheDocument();
  });

  it('MLX preset button fills base_url and switches provider to openai_compatible', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /mlx/i }));
    });

    const select = screen.getByRole('combobox', { name: /provider/i });
    expect((select as HTMLSelectElement).value).toBe('openai_compatible');
    expect(screen.getByDisplayValue('http://127.0.0.1:8080/v1')).toBeInTheDocument();
  });

  it('Ollama preset button fills base_url and switches provider to openai_compatible', async () => {
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /ollama/i }));
    });

    const select = screen.getByRole('combobox', { name: /provider/i });
    expect((select as HTMLSelectElement).value).toBe('openai_compatible');
    expect(screen.getByDisplayValue('http://localhost:11434/v1')).toBeInTheDocument();
  });

  it('Save button PUT includes llm payload with provider', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    expect(mockApiPut).toHaveBeenCalledOnce();
    const [, payload] = mockApiPut.mock.calls[0] as [string, Record<string, unknown>];
    expect(payload).toHaveProperty('llm');
    expect((payload.llm as Record<string, unknown>).provider).toBe('claude_cli');
  });

  it('Save button PUT includes updated llm.base_url after MLX preset click', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /mlx/i }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    expect(mockApiPut).toHaveBeenCalledOnce();
    const [, payload] = mockApiPut.mock.calls[0] as [string, Record<string, unknown>];
    const llm = payload.llm as Record<string, unknown>;
    expect(llm.provider).toBe('openai_compatible');
    expect(llm.base_url).toBe('http://127.0.0.1:8080/v1');
  });
});
