import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  createTestChatDiaryEntry,
  createTestChatSession,
  createUserMemory,
  clearTestChatSession,
  deleteMemory,
  deleteTestChatDiaryEntry,
  deleteTestChatSession,
  getTestChatDiary,
  getTestChatMeta,
  getTestChatSession,
  getTestChatSessions,
  getUserMemories,
  sendTestChatMessage,
  updateTestChatDiaryEntry,
  updateTestChatSession,
  MemoryItem,
  TestChatDiaryEntry,
  TestChatMessage,
  TestChatMeta,
  TestChatSession,
} from '../api/client';

type DiaryCategory = 'day_entries' | 'reflection_entries';

const CATEGORY_LABELS: Record<string, string> = {
  day_entries: 'события дня',
  reflection_entries: 'мысли и чувства',
};

const styles: Record<string, React.CSSProperties> = {
  page: {
    display: 'flex',
    gap: '16px',
    height: 'calc(100vh - 48px)',
    minHeight: 0,
    boxSizing: 'border-box' as const,
  },
  sidebar: {
    width: '300px',
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: '10px',
    minHeight: 0,
  },
  panel: {
    background: 'var(--bg-card)',
    border: '1px solid var(--border)',
    borderRadius: '12px',
    minHeight: 0,
  },
  sidebarList: {
    flex: 1,
    overflowY: 'auto',
    padding: '8px',
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
  },
  newBox: { display: 'flex', flexDirection: 'column', gap: '6px', padding: '8px' },
  input: {
    width: '100%',
    boxSizing: 'border-box' as const,
    padding: '7px 9px',
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    color: 'var(--text-primary)',
    fontSize: '13px',
  },
  select: {
    padding: '7px 9px',
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    color: 'var(--text-primary)',
    fontSize: '13px',
  },
  btn: {
    padding: '7px 12px',
    background: 'var(--accent)',
    color: '#fff',
    border: 'none',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '13px',
    fontWeight: 600,
    flexShrink: 0,
  },
  btnGhost: {
    padding: '5px 9px',
    background: 'var(--bg-hover)',
    color: 'var(--text-primary)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '12px',
    flexShrink: 0,
  },
  btnDanger: {
    padding: '4px 8px',
    background: 'transparent',
    color: 'var(--error)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '12px',
    flexShrink: 0,
  },
  sessionItem: {
    display: 'block',
    width: '100%',
    textAlign: 'left' as const,
    background: 'var(--bg-secondary)',
    border: '1px solid transparent',
    borderRadius: '8px',
    padding: '9px 10px',
    cursor: 'pointer',
    color: 'var(--text-primary)',
  },
  sessionItemActive: { borderColor: 'var(--accent)', background: 'var(--bg-hover)' },
  sessionTitle: {
    fontSize: '13px',
    fontWeight: 600,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  },
  sessionPreview: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
    marginTop: '2px',
  },
  sessionMeta: {
    fontSize: '10px',
    color: 'var(--text-secondary)',
    marginTop: '4px',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: '6px',
  },
  chat: {
    flex: 1,
    minWidth: 0,
    minHeight: 0,
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--bg-card)',
    border: '1px solid var(--border)',
    borderRadius: '12px',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '10px 14px',
    borderBottom: '1px solid var(--border)',
    flexWrap: 'wrap' as const,
  },
  headerTitle: { fontSize: '16px', fontWeight: 700, color: 'var(--text-primary)' },
  spacer: { flex: 1 },
  badge: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: '10px',
    fontSize: '11px',
    fontWeight: 600,
    background: 'rgba(108,92,231,0.2)',
    color: 'var(--accent)',
  },
  badgeMuted: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: '10px',
    fontSize: '11px',
    background: 'var(--bg-hover)',
    color: 'var(--text-secondary)',
    fontFamily: 'monospace',
  },
  settings: {
    display: 'flex',
    gap: '8px',
    alignItems: 'flex-end',
    padding: '10px 14px',
    borderBottom: '1px solid var(--border)',
    flexWrap: 'wrap' as const,
    background: 'var(--bg-secondary)',
  },
  field: { display: 'flex', flexDirection: 'column', gap: '3px', minWidth: '140px' },
  label: { fontSize: '10px', color: 'var(--text-secondary)', textTransform: 'uppercase' as const, letterSpacing: '0.5px' },
  hint: { fontSize: '10px', color: 'var(--text-secondary)', maxWidth: '260px' },
  messages: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    padding: '16px',
    display: 'flex',
    flexDirection: 'column',
    gap: '10px',
  },
  empty: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: 'var(--text-secondary)',
    fontSize: '13px',
    textAlign: 'center' as const,
    padding: '32px',
  },
  bubbleRow: { display: 'flex' },
  bubbleRowUser: { justifyContent: 'flex-end' },
  bubble: {
    maxWidth: '78%',
    padding: '9px 12px',
    borderRadius: '12px',
    background: 'var(--bg-secondary)',
    color: 'var(--text-primary)',
    fontSize: '14px',
    whiteSpace: 'pre-wrap' as const,
    wordBreak: 'break-word' as const,
    border: '1px solid var(--border)',
  },
  bubbleUser: { background: 'var(--accent)', color: '#fff', borderColor: 'transparent' },
  note: { alignSelf: 'center', fontSize: '11px', color: 'var(--text-secondary)', fontStyle: 'italic' },
  noteError: { alignSelf: 'center', fontSize: '12px', color: 'var(--error)' },
  thinking: { alignSelf: 'flex-start', fontSize: '12px', color: 'var(--text-secondary)', padding: '6px 12px' },
  composer: {
    display: 'flex',
    gap: '8px',
    padding: '12px 14px',
    borderTop: '1px solid var(--border)',
    alignItems: 'flex-end',
  },
  textarea: {
    flex: 1,
    minHeight: '44px',
    maxHeight: '160px',
    resize: 'vertical' as const,
    boxSizing: 'border-box' as const,
    padding: '10px 12px',
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border)',
    borderRadius: '8px',
    color: 'var(--text-primary)',
    fontSize: '14px',
    fontFamily: 'inherit',
  },
  footerHint: { fontSize: '10px', color: 'var(--text-secondary)', padding: '0 14px 8px' },
  error: { color: 'var(--error)', fontSize: '12px', padding: '0 14px 8px' },
  bar: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '7px 14px',
    borderBottom: '1px solid var(--border)',
    fontSize: '11px',
    color: 'var(--text-secondary)',
    flexWrap: 'wrap' as const,
  },
  drawer: {
    padding: '10px 14px 14px',
    borderBottom: '1px solid var(--border)',
    background: 'var(--bg-secondary)',
    maxHeight: '260px',
    overflowY: 'auto',
  },
  list: { display: 'flex', flexDirection: 'column', gap: '6px' },
  item: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: '8px',
    fontSize: '12px',
    color: 'var(--text-primary)',
    background: 'var(--bg-card)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '6px 8px',
  },
  itemText: { flex: 1, wordBreak: 'break-word' as const, whiteSpace: 'pre-wrap' as const },
  itemMeta: { fontSize: '10px', color: 'var(--text-secondary)', marginBottom: '2px' },
  addRow: { display: 'flex', gap: '6px', marginTop: '8px', alignItems: 'flex-start' },
  debug: { marginTop: '6px', fontSize: '11px', color: 'var(--text-secondary)' },
  debugSummary: { cursor: 'pointer', userSelect: 'none' as const },
  debugSection: { marginTop: '8px' },
  debugLabel: {
    fontSize: '10px',
    textTransform: 'uppercase' as const,
    letterSpacing: '1px',
    color: 'var(--accent)',
    marginBottom: '4px',
  },
  debugRow: { display: 'flex', gap: '6px', alignItems: 'baseline', marginBottom: '3px', flexWrap: 'wrap' as const },
  debugChip: { fontFamily: 'monospace', background: 'var(--bg-hover)', borderRadius: '4px', padding: '1px 5px' },
  debugMuted: { color: 'var(--text-secondary)' },
  debugError: { color: 'var(--error)', marginTop: '4px' },
  debugIter: { borderTop: '1px solid var(--border)', paddingTop: '6px', marginTop: '6px' },
  debugText: { color: 'var(--text-primary)', whiteSpace: 'pre-wrap' as const, marginTop: '2px' },
  debugReasoning: { color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' as const, marginTop: '2px', opacity: 0.8 },
  debugCode: {
    display: 'block',
    fontFamily: 'monospace',
    background: 'var(--bg-secondary)',
    borderRadius: '4px',
    padding: '3px 5px',
    marginTop: '3px',
    whiteSpace: 'pre-wrap' as const,
    wordBreak: 'break-word' as const,
  },
  pre: {
    maxHeight: '280px',
    overflow: 'auto',
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '8px',
    fontSize: '11px',
    whiteSpace: 'pre-wrap' as const,
    wordBreak: 'break-word' as const,
    color: 'var(--text-primary)',
  },
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function formatWhen(value: string | null): string {
  if (!value) return '';
  const d = new Date(value.includes('T') ? value : value.replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

function DebugBlock({ debug }: { debug: Record<string, unknown> }) {
  const timing = asRecord(debug.timing);
  const actions = asArray(debug.actions);
  const iterations = asArray(debug.iterations);
  const systemPrompt = typeof debug.system_prompt === 'string' ? debug.system_prompt : '';
  const error = typeof debug.error === 'string' ? debug.error : '';
  const total = timing && timing.total ? String(timing.total) : '';

  return (
    <details style={styles.debug}>
      <summary style={styles.debugSummary}>
        debug · {iterations.length} итер. · {actions.length} тул.
        {total ? ` · ${total}` : ''}
        {error ? ' · error' : ''}
      </summary>

      {error && <div style={styles.debugError}>{error}</div>}

      {actions.length > 0 && (
        <div style={styles.debugSection}>
          <div style={styles.debugLabel}>Инструменты</div>
          {actions.map((action, i) => {
            const rec = asRecord(action) || {};
            return (
              <div key={i} style={styles.debugRow}>
                <span style={styles.debugChip}>{String(rec.type || '?')}</span>
                <span style={styles.debugMuted}>{String(rec.status || '')}</span>
                {rec.error ? <span style={styles.debugError}>{String(rec.error)}</span> : null}
              </div>
            );
          })}
        </div>
      )}

      {iterations.length > 0 && (
        <div style={styles.debugSection}>
          <div style={styles.debugLabel}>Итерации</div>
          {iterations.map((iteration, i) => {
            const rec = asRecord(iteration) || {};
            const calls = asArray(rec.tool_calls);
            return (
              <div key={i} style={styles.debugIter}>
                <div style={styles.debugMuted}>
                  #{String(rec.iteration ?? i + 1)} · {String(rec.finish_reason ?? '')}
                </div>
                {rec.thoughts ? <div style={styles.debugText}>{String(rec.thoughts)}</div> : null}
                {rec.reasoning ? (
                  <div style={styles.debugReasoning}>{String(rec.reasoning)}</div>
                ) : null}
                {calls.map((call, j) => {
                  const cr = asRecord(call) || {};
                  return (
                    <code key={j} style={styles.debugCode}>
                      {String(cr.name || '?')}({String(cr.arguments || '')})
                    </code>
                  );
                })}
              </div>
            );
          })}
        </div>
      )}

      {systemPrompt && (
        <details style={styles.debugSection}>
          <summary style={styles.debugSummary}>system prompt ({systemPrompt.length} симв.)</summary>
          <pre style={styles.pre}>{systemPrompt}</pre>
        </details>
      )}
    </details>
  );
}

function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] || category;
}

export default function TestChat() {
  const [meta, setMeta] = useState<TestChatMeta | null>(null);
  const [sessions, setSessions] = useState<TestChatSession[]>([]);
  const [session, setSession] = useState<TestChatSession | null>(null);
  const [messages, setMessages] = useState<TestChatMessage[]>([]);
  const [newTitle, setNewTitle] = useState('');
  const [newName, setNewName] = useState('');
  const [newState, setNewState] = useState('dialog');
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [savingSettings, setSavingSettings] = useState(false);

  const [editTitle, setEditTitle] = useState('');
  const [editName, setEditName] = useState('');
  const [editState, setEditState] = useState('dialog');

  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [memoryText, setMemoryText] = useState('');
  const [memoryBusy, setMemoryBusy] = useState(false);

  const [diaryOpen, setDiaryOpen] = useState(false);
  const [diary, setDiary] = useState<TestChatDiaryEntry[]>([]);
  const [diaryNewText, setDiaryNewText] = useState('');
  const [diaryNewCategory, setDiaryNewCategory] = useState<DiaryCategory>('day_entries');
  const [diaryEditId, setDiaryEditId] = useState<number | null>(null);
  const [diaryEditText, setDiaryEditText] = useState('');
  const [diaryEditCategory, setDiaryEditCategory] = useState<DiaryCategory>('day_entries');
  const [diaryBusy, setDiaryBusy] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);

  const syncSession = useCallback((next: TestChatSession) => {
    setSession(next);
    setEditTitle(next.title);
    setEditName(next.user_name);
    setEditState(next.state);
  }, []);

  const loadSessions = useCallback(async () => {
    const rows = await getTestChatSessions();
    setSessions(rows);
    return rows;
  }, []);

  const loadMemories = useCallback(async (userId: string) => {
    const data = await getUserMemories(userId, undefined, 1, 100);
    setMemories(data.items);
  }, []);

  const loadDiary = useCallback(async (sessionId: string) => {
    const rows = await getTestChatDiary(sessionId);
    setDiary(rows);
  }, []);

  const loadSession = useCallback(async (sessionId: string) => {
    const detail = await getTestChatSession(sessionId);
    syncSession(detail.session);
    setMessages(detail.messages);
    await Promise.all([
      loadMemories(detail.session.user_id),
      loadDiary(sessionId),
    ]);
  }, [loadDiary, loadMemories, syncSession]);

  useEffect(() => {
    (async () => {
      try {
        const [m, rows] = await Promise.all([getTestChatMeta(), getTestChatSessions()]);
        setMeta(m);
        setNewName(m.default_user_name);
        setNewState(m.default_state);
        setSessions(rows);
        if (rows.length > 0) {
          await loadSession(rows[0].session_id);
        }
      } catch (e: unknown) {
        setError(`Не удалось загрузить тест-чат: ${e instanceof Error ? e.message : String(e)}`);
      }
    })();
  }, [loadSession]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, sending]);

  const selectSession = async (sessionId: string) => {
    setError('');
    try {
      await loadSession(sessionId);
      setDiaryOpen(false);
      setMemoryOpen(false);
    } catch (e: unknown) {
      setError(`Не удалось загрузить диалог: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const createSession = async () => {
    try {
      setError('');
      const created = await createTestChatSession({
        title: newTitle.trim() || undefined,
        user_name: newName.trim() || undefined,
        state: newState,
      });
      setNewTitle('');
      await loadSessions();
      await loadSession(created.session_id);
    } catch (e: unknown) {
      setError(`Не удалось создать диалог: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const removeSession = async (sessionId: string) => {
    if (!window.confirm('Удалить тестовый диалог? История, дневник и debug-данные исчезнут безвозвратно.')) {
      return;
    }
    try {
      setError('');
      await deleteTestChatSession(sessionId);
      const rows = await loadSessions();
      if (session?.session_id === sessionId) {
        const next = rows[0]?.session_id ?? null;
        if (next) {
          await loadSession(next);
        } else {
          setSession(null);
          setMessages([]);
          setMemories([]);
          setDiary([]);
        }
      }
    } catch (e: unknown) {
      setError(`Не удалось удалить диалог: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const clearMessages = async () => {
    if (!session) return;
    if (!window.confirm('Очистить сообщения? Дневник и настройки сессии останутся.')) return;
    try {
      setError('');
      await clearTestChatSession(session.session_id);
      setMessages([]);
      await loadSessions();
    } catch (e: unknown) {
      setError(`Не удалось очистить диалог: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const saveSettings = async () => {
    if (!session) return;
    setSavingSettings(true);
    try {
      setError('');
      const updated = await updateTestChatSession(session.session_id, {
        title: editTitle.trim() || undefined,
        user_name: editName.trim() || undefined,
        state: editState,
      });
      syncSession(updated);
      await loadSessions();
      await loadMemories(updated.user_id);
    } catch (e: unknown) {
      setError(`Не удалось сохранить настройки: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSavingSettings(false);
    }
  };

  const applyState = async (state: string) => {
    setEditState(state);
    if (!session) return;
    try {
      setError('');
      const updated = await updateTestChatSession(session.session_id, { state });
      // Do not touch editName/editTitle here: unsaved text should survive.
      setSession(updated);
      await loadSessions();
    } catch (e: unknown) {
      setError(`Не удалось изменить состояние: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || !session || sending) return;
    const optimistic: TestChatMessage = {
      id: -Date.now(),
      role: 'user',
      kind: 'text',
      content: text,
      created_at: new Date().toISOString(),
      debug: null,
    };
    setInput('');
    setError('');
    setSending(true);
    setMessages(prev => [...prev, optimistic]);
    try {
      const result = await sendTestChatMessage(session.session_id, text);
      setMessages(prev => [
        ...prev.filter(m => m.id !== optimistic.id),
        ...result.messages,
      ]);
      if (result.session) {
        // A turn can only change the state; keep unsaved name/title edits.
        setSession(result.session);
        setEditState(result.session.state);
      }
      await loadSessions();
      await loadDiary(session.session_id);
    } catch (e: unknown) {
      setError(`Ошибка хода: ${e instanceof Error ? e.message : String(e)}`);
      try {
        await loadSession(session.session_id);
      } catch {
        /* keep the optimistic view */
      }
    } finally {
      setSending(false);
    }
  };

  const onComposerKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const addMemory = async () => {
    const content = memoryText.trim();
    if (!content || !session) return;
    setMemoryBusy(true);
    try {
      setError('');
      await createUserMemory(session.user_id, { content });
      setMemoryText('');
      await loadMemories(session.user_id);
    } catch (e: unknown) {
      setError(`Не удалось добавить память: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setMemoryBusy(false);
    }
  };

  const removeMemory = async (memId: string) => {
    if (!session) return;
    try {
      setError('');
      await deleteMemory(memId);
      await loadMemories(session.user_id);
    } catch (e: unknown) {
      setError(`Не удалось удалить память: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const addDiary = async () => {
    const content = diaryNewText.trim();
    if (!content || !session) return;
    setDiaryBusy(true);
    try {
      setError('');
      await createTestChatDiaryEntry(session.session_id, {
        content, category: diaryNewCategory,
      });
      setDiaryNewText('');
      await loadDiary(session.session_id);
    } catch (e: unknown) {
      setError(`Не удалось добавить запись: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDiaryBusy(false);
    }
  };

  const startDiaryEdit = (entry: TestChatDiaryEntry) => {
    setDiaryEditId(entry.id);
    setDiaryEditText(entry.content);
    setDiaryEditCategory(entry.category as DiaryCategory);
  };

  const saveDiaryEdit = async () => {
    const content = diaryEditText.trim();
    if (!content || !session || diaryEditId === null) return;
    setDiaryBusy(true);
    try {
      setError('');
      await updateTestChatDiaryEntry(session.session_id, diaryEditId, {
        content, category: diaryEditCategory,
      });
      setDiaryEditId(null);
      await loadDiary(session.session_id);
    } catch (e: unknown) {
      setError(`Не удалось сохранить запись: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDiaryBusy(false);
    }
  };

  const removeDiary = async (entryId: number) => {
    if (!session) return;
    if (!window.confirm('Удалить запись из тестового дневника?')) return;
    try {
      setError('');
      await deleteTestChatDiaryEntry(session.session_id, entryId);
      await loadDiary(session.session_id);
    } catch (e: unknown) {
      setError(`Не удалось удалить запись: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const states = meta?.states?.length ? meta.states : ['dialog', 'rest', 'working'];

  return (
    <div style={styles.page}>
      <aside style={styles.sidebar}>
        <div style={{ ...styles.panel, ...styles.newBox }}>
          <input
            style={styles.input}
            placeholder="Сценарий (необязательно)"
            value={newTitle}
            onChange={e => setNewTitle(e.target.value)}
            maxLength={120}
          />
          <input
            style={styles.input}
            placeholder="Имя собеседника"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            maxLength={80}
          />
          <div style={{ display: 'flex', gap: '6px' }}>
            <select
              style={{ ...styles.select, flex: 1 }}
              value={newState}
              onChange={e => setNewState(e.target.value)}
              title="Начальное состояние Милы"
            >
              {states.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <button style={styles.btn} onClick={createSession}>+</button>
          </div>
        </div>

        <div style={{ ...styles.panel, ...styles.sidebarList }}>
          {sessions.length === 0 && (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', padding: '8px' }}>
              Пока нет тестовых диалогов.
            </div>
          )}
          {sessions.map(item => (
            <div
              key={item.session_id}
              style={{
                ...styles.sessionItem,
                ...(item.session_id === session?.session_id ? styles.sessionItemActive : {}),
              }}
              onClick={() => selectSession(item.session_id)}
              role="button"
              tabIndex={0}
              onKeyDown={e => { if (e.key === 'Enter') selectSession(item.session_id); }}
            >
              <div style={styles.sessionTitle}>{item.title || 'Без названия'}</div>
              {item.preview && <div style={styles.sessionPreview}>{item.preview}</div>}
              <div style={styles.sessionMeta}>
                <span>{item.user_name} · {item.state} · {item.message_count} сообщ.</span>
                <button
                  style={styles.btnDanger}
                  onClick={e => { e.stopPropagation(); removeSession(item.session_id); }}
                  title="Удалить диалог"
                >
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      </aside>

      <section style={styles.chat}>
        {!session ? (
          <div style={styles.empty}>
            Создай тестовый диалог слева. Мила будет в выбранном состоянии,
            сможет читать память собеседника и писать в свой тестовый дневник.
          </div>
        ) : (
          <>
            <div style={styles.header}>
              <div style={styles.headerTitle}>{session.title || 'Тестовый диалог'}</div>
              <span style={styles.badge}>{session.state}</span>
              <span style={styles.badgeMuted}>{session.alias}</span>
              <span style={styles.badgeMuted} title="user_id для памяти">{session.user_id}</span>
              <div style={styles.spacer} />
              <button style={styles.btnGhost} onClick={clearMessages}>Очистить сообщения</button>
            </div>

            <div style={styles.settings}>
              <div style={styles.field}>
                <span style={styles.label}>Сценарий</span>
                <input
                  style={styles.input}
                  value={editTitle}
                  onChange={e => setEditTitle(e.target.value)}
                  maxLength={120}
                />
              </div>
              <div style={styles.field}>
                <span style={styles.label}>Имя собеседника</span>
                <input
                  style={styles.input}
                  value={editName}
                  onChange={e => setEditName(e.target.value)}
                  maxLength={80}
                />
              </div>
              <div style={styles.field}>
                <span style={styles.label}>Состояние</span>
                <select
                  style={styles.select}
                  value={editState}
                  onChange={e => applyState(e.target.value)}
                  disabled={sending}
                >
                  {states.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <button
                style={{ ...styles.btn, opacity: savingSettings ? 0.6 : 1 }}
                onClick={saveSettings}
                disabled={savingSettings}
              >
                Сохранить
              </button>
              <span style={styles.hint}>
                Имя задаёт user_id и алиас. Смена имени переключает память собеседника.
              </span>
            </div>

            <div style={styles.bar}>
              <button
                style={styles.btnGhost}
                onClick={() => {
                  setMemoryOpen(v => !v);
                  if (!memoryOpen) loadMemories(session.user_id);
                }}
              >
                {memoryOpen ? '▾' : '▸'} Память ({memories.length})
              </button>
              <button
                style={styles.btnGhost}
                onClick={() => {
                  setDiaryOpen(v => !v);
                  if (!diaryOpen) loadDiary(session.session_id);
                }}
              >
                {diaryOpen ? '▾' : '▸'} Тестовый дневник ({diary.length})
              </button>
              <span>не сохраняется в реальную память и удаляется вместе с сессией</span>
            </div>

            {memoryOpen && (
              <div style={styles.drawer}>
                <div style={styles.list}>
                  {memories.length === 0 && (
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                      Память пуста — search_memory ничего не найдёт.
                    </div>
                  )}
                  {memories.map(mem => (
                    <div key={mem.mem_id} style={styles.item}>
                      <span style={styles.itemText}>{mem.content}</span>
                      <button
                        style={styles.btnDanger}
                        onClick={() => removeMemory(mem.mem_id)}
                        title="Удалить память"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
                <div style={styles.addRow}>
                  <input
                    style={styles.input}
                    placeholder="Новый факт о собеседнике"
                    value={memoryText}
                    onChange={e => setMemoryText(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') addMemory(); }}
                    disabled={memoryBusy}
                  />
                  <button style={styles.btn} onClick={addMemory} disabled={memoryBusy}>
                    Добавить
                  </button>
                </div>
              </div>
            )}

            {diaryOpen && (
              <div style={styles.drawer}>
                <div style={styles.list}>
                  {diary.length === 0 && (
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                      Дневник пуст. Мила может писать сюда через write_diary.
                    </div>
                  )}
                  {diary.map(entry => (
                    <div key={entry.id} style={styles.item}>
                      <div style={{ flex: 1 }}>
                        <div style={styles.itemMeta}>
                          #{entry.entry_number} · {categoryLabel(entry.category)}
                          · ID {entry.id} · {formatWhen(entry.updated_at)}
                        </div>
                        {diaryEditId === entry.id ? (
                          <>
                            <textarea
                              style={{ ...styles.textarea, minHeight: '60px', fontSize: '12px' }}
                              value={diaryEditText}
                              onChange={e => setDiaryEditText(e.target.value)}
                            />
                            <div style={{ display: 'flex', gap: '6px', marginTop: '4px' }}>
                              <select
                                style={styles.select}
                                value={diaryEditCategory}
                                onChange={e => setDiaryEditCategory(e.target.value as DiaryCategory)}
                              >
                                <option value="day_entries">события дня</option>
                                <option value="reflection_entries">мысли и чувства</option>
                              </select>
                              <button style={styles.btn} onClick={saveDiaryEdit} disabled={diaryBusy}>
                                Сохранить
                              </button>
                              <button style={styles.btnGhost} onClick={() => setDiaryEditId(null)}>
                                Отмена
                              </button>
                            </div>
                          </>
                        ) : (
                          <div style={styles.itemText}>{entry.content}</div>
                        )}
                      </div>
                      {diaryEditId !== entry.id && (
                        <>
                          <button style={styles.btnGhost} onClick={() => startDiaryEdit(entry)}>
                            Изменить
                          </button>
                          <button style={styles.btnDanger} onClick={() => removeDiary(entry.id)}>
                            ✕
                          </button>
                        </>
                      )}
                    </div>
                  ))}
                </div>
                <div style={styles.addRow}>
                  <textarea
                    style={{ ...styles.textarea, minHeight: '54px', fontSize: '12px' }}
                    placeholder="Добавить запись в тестовый дневник"
                    value={diaryNewText}
                    onChange={e => setDiaryNewText(e.target.value)}
                    disabled={diaryBusy}
                  />
                  <select
                    style={styles.select}
                    value={diaryNewCategory}
                    onChange={e => setDiaryNewCategory(e.target.value as DiaryCategory)}
                  >
                    <option value="day_entries">события дня</option>
                    <option value="reflection_entries">мысли и чувства</option>
                  </select>
                  <button style={styles.btn} onClick={addDiary} disabled={diaryBusy}>
                    Добавить
                  </button>
                </div>
              </div>
            )}

            <div style={styles.messages}>
              {messages.length === 0 && !sending && (
                <div style={styles.note}>
                  Напиши первое сообщение — история начнётся с пустого места.
                </div>
              )}
              {messages.map(message => {
                if (message.kind === 'silence') {
                  const reason = message.debug && typeof message.debug.silence_reason === 'string'
                    ? message.debug.silence_reason
                    : 'empty reply';
                  return (
                    <div key={message.id} style={styles.note}>
                      Мила промолчала ({reason})
                      {message.debug ? <DebugBlock debug={message.debug} /> : null}
                    </div>
                  );
                }
                if (message.kind === 'error') {
                  const err = message.debug && typeof message.debug.error === 'string'
                    ? message.debug.error
                    : 'pipeline error';
                  return (
                    <div key={message.id} style={styles.noteError}>
                      Ошибка хода: {err}
                      {message.debug ? <DebugBlock debug={message.debug} /> : null}
                    </div>
                  );
                }
                const isUser = message.role === 'user';
                return (
                  <div
                    key={message.id}
                    style={{ ...styles.bubbleRow, ...(isUser ? styles.bubbleRowUser : {}) }}
                  >
                    <div style={{ ...styles.bubble, ...(isUser ? styles.bubbleUser : {}) }}>
                      {message.content}
                      {!isUser && message.debug ? <DebugBlock debug={message.debug} /> : null}
                    </div>
                  </div>
                );
              })}
              {sending && <div style={styles.thinking}>Мила думает…</div>}
              <div ref={bottomRef} />
            </div>

            {error && <div style={styles.error}>{error}</div>}
            <div style={styles.composer}>
              <textarea
                style={styles.textarea}
                placeholder="Написать Миле… (Enter — отправить, Shift+Enter — новая строка)"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onComposerKeyDown}
                disabled={sending}
                rows={2}
              />
              <button
                style={{ ...styles.btn, opacity: sending || !input.trim() ? 0.5 : 1 }}
                onClick={send}
                disabled={sending || !input.trim()}
              >
                {sending ? '…' : 'Отправить'}
              </button>
            </div>
            <div style={styles.footerHint}>
              Сообщения не попадают в chunks, контекст реальных чатов, память,
              реальный дневник или presence. Удаление сессии стирает историю и тестовый дневник.
            </div>
          </>
        )}
      </section>
    </div>
  );
}
