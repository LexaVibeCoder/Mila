import React, { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  getUser, updateUser, getUserMemories, createUserMemory, getUserMessages,
  updateMemory, deleteMemory, getPipelineLogs, getAttitudeHistory, generatePortrait,
  UserDetail, MemoryItem, UserMessageItem, PipelineLogItem, AttitudePoint,
} from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1100px' },
  back: { background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '13px', marginBottom: '12px', padding: 0 },
  header: { display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '20px', flexWrap: 'wrap' as const },
  avatar: { width: '56px', height: '56px', borderRadius: '50%', background: 'var(--accent)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '22px', fontWeight: 700, flexShrink: 0 },
  title: { fontSize: '24px', fontWeight: 700, color: 'var(--text-primary)' },
  sub: { fontSize: '12px', color: 'var(--text-secondary)', fontFamily: 'monospace' },
  grid: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' },
  card: { background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '12px', padding: '20px' },
  cardFull: { background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '12px', padding: '20px', marginBottom: '16px' },
  cardTitle: { fontSize: '13px', fontWeight: 700, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '16px' },
  stats: { display: 'flex', gap: '24px', marginBottom: '16px', flexWrap: 'wrap' as const },
  stat: { textAlign: 'center' as const },
  statNum: { fontSize: '22px', fontWeight: 700, color: 'var(--text-primary)' },
  statLabel: { fontSize: '11px', color: 'var(--text-secondary)', textTransform: 'uppercase' as const, letterSpacing: '1px' },
  field: { marginBottom: '12px' },
  label: { display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' },
  input: { width: '100%', boxSizing: 'border-box' as const, padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px' },
  textarea: { width: '100%', boxSizing: 'border-box' as const, padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', minHeight: '70px', fontFamily: 'inherit' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '6px 12px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '12px' },
  btnDanger: { padding: '4px 10px', background: 'var(--error)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '12px' },
  presetRow: { display: 'flex', gap: '8px', flexWrap: 'wrap' as const, marginBottom: '12px' },
  slider: { width: '100%', accentColor: 'var(--accent)' },
  memItem: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '10px 12px', marginBottom: '8px', border: '1px solid var(--border)', fontSize: '13px' },
  msgItem: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '10px 12px', marginBottom: '8px', border: '1px solid var(--border)' },
  msgMeta: { fontSize: '11px', color: 'var(--text-secondary)', marginBottom: '4px' },
  msgBody: { fontSize: '13px', whiteSpace: 'pre-wrap' as const, wordBreak: 'break-word' as const, color: 'var(--text-primary)' },
  badge: { display: 'inline-block', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: 600, background: 'rgba(108,92,231,0.2)', color: 'var(--accent)' },
  toolbar: { display: 'flex', gap: '8px', marginBottom: '12px' },
  logItem: { display: 'block', width: '100%', boxSizing: 'border-box' as const, textAlign: 'left' as const, background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '8px', padding: '10px 12px', marginBottom: '8px', cursor: 'pointer', color: 'var(--text-primary)', fontSize: '13px' },
};

const RELATIONSHIP_OPTIONS = [
  'friend', 'close_friend', 'family', 'partner',
  'acquaintance', 'colleague', 'stranger', 'blocked',
];

const SENTIMENT_PRESETS = [
  { label: 'Друг', value: 0.8 },
  { label: 'Тёплый', value: 0.4 },
  { label: 'Нейтрально', value: 0.0 },
  { label: 'Холодный', value: -0.4 },
  { label: 'Враждебный', value: -0.8 },
];

function closenessLabel(s: number): string {
  if (s > 0.6) return 'Друг';
  if (s > 0.2) return 'Тёплый знакомый';
  if (s < -0.6) return 'Холодный чужой';
  if (s < -0.2) return 'Холодный знакомый';
  return 'Нейтрально';
}

function AttitudeChart({ points }: { points: AttitudePoint[] }) {
  if (points.length === 0) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '12px' }}>
        История начнёт собираться с первого изменения отношения.
      </div>
    );
  }
  const W = 300;
  const H = 96;
  const PAD = 10;
  const x = (i: number) =>
    points.length === 1 ? W / 2 : PAD + (i * (W - 2 * PAD)) / (points.length - 1);
  const y = (s: number) => {
    const c = Math.max(-1, Math.min(1, s));
    return PAD + ((1 - c) / 2) * (H - 2 * PAD);
  };
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.sentiment).toFixed(1)}`).join(' ');
  const last = points[points.length - 1];
  return (
    <div style={{ marginTop: '12px' }}>
      <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
        Динамика отношения ({points.length} изм.)
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
        <line x1={PAD} y1={y(0)} x2={W - PAD} y2={y(0)} stroke="var(--border)" strokeDasharray="3 3" strokeWidth="1" />
        <text x={W - PAD} y={y(0) - 3} fontSize="8" fill="var(--text-secondary)" textAnchor="end">0</text>
        <text x={W - PAD} y={y(1) + 3} fontSize="8" fill="var(--text-secondary)" textAnchor="end">+1</text>
        <text x={W - PAD} y={y(-1) + 3} fontSize="8" fill="var(--text-secondary)" textAnchor="end">−1</text>
        <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" />
        {points.map((p, i) => (
          <circle key={i} cx={x(i)} cy={y(p.sentiment)} r="3" fill="var(--accent)">
            <title>{`${p.sentiment.toFixed(2)} · ${p.created_at ? p.created_at.slice(0, 16).replace('T', ' ') : ''}`}</title>
          </circle>
        ))}
      </svg>
      <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>
        {points[0].created_at ? points[0].created_at.slice(0, 10) : ''} → {last.created_at ? last.created_at.slice(0, 16).replace('T', ' ') : 'сейчас'}
      </div>
    </div>
  );
}

export default function UserDetailPage() {
  const { user_id } = useParams<{ user_id: string }>();
  const navigate = useNavigate();
  const [user, setUser] = useState<UserDetail | null>(null);
  const [msg, setMsg] = useState('');
  const [saving, setSaving] = useState(false);

  // attitude form
  const [sentiment, setSentiment] = useState(0);
  const [relType, setRelType] = useState('friend');
  // profile form
  const [preferredName, setPreferredName] = useState('');
  const [userName, setUserName] = useState('');
  const [appearance, setAppearance] = useState('');

  // memories tab
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [memTotal, setMemTotal] = useState(0);
  const [memPage, setMemPage] = useState(1);
  const [memSearch, setMemSearch] = useState('');
  const [newMem, setNewMem] = useState('');
  const [editMemId, setEditMemId] = useState<string | null>(null);
  const [editMemText, setEditMemText] = useState('');

  // messages tab
  const [messages, setMessages] = useState<UserMessageItem[]>([]);
  const [msgTotal, setMsgTotal] = useState(0);
  const [msgPage, setMsgPage] = useState(1);

  // pipeline logs for this user
  const [logs, setLogs] = useState<PipelineLogItem[]>([]);

  // attitude history + portrait
  const [history, setHistory] = useState<AttitudePoint[]>([]);
  const [portrait, setPortrait] = useState('');
  const [portraitMeta, setPortraitMeta] = useState('');
  const [portraitLoading, setPortraitLoading] = useState(false);

  const loadUser = useCallback(async () => {
    if (!user_id) return;
    try {
      const u = await getUser(user_id);
      setUser(u);
      setSentiment(u.sentiment);
      setRelType(u.relationship_type);
      setPreferredName(u.preferred_name || '');
      setUserName(u.user_name || '');
      setAppearance(u.appearance_description || '');
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    }
  }, [user_id]);

  const loadMemories = useCallback(async () => {
    if (!user_id) return;
    try {
      const r = await getUserMemories(user_id, memSearch || undefined, memPage, 10);
      setMemories(r.items);
      setMemTotal(r.total);
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    }
  }, [user_id, memSearch, memPage]);

  const loadMessages = useCallback(async () => {
    if (!user_id) return;
    try {
      const r = await getUserMessages(user_id, msgPage, 10);
      setMessages(r.items);
      setMsgTotal(r.total);
    } catch { /* history may be empty — not fatal */ }
  }, [user_id, msgPage]);

  const loadLogs = useCallback(async () => {
    if (!user_id) return;
    try {
      const r = await getPipelineLogs(1, 100);
      setLogs(r.items.filter(l => l.user_id === user_id).slice(0, 5));
    } catch { /* ignore */ }
  }, [user_id]);

  const loadHistory = useCallback(async () => {
    if (!user_id) return;
    try {
      const r = await getAttitudeHistory(user_id);
      setHistory(r.items);
    } catch { /* history table may be new — not fatal */ }
  }, [user_id]);

  useEffect(() => { loadUser(); }, [loadUser]);
  useEffect(() => {
    const t = setTimeout(loadMemories, memSearch ? 300 : 0);
    return () => clearTimeout(t);
  }, [loadMemories]);
  useEffect(() => { loadMessages(); }, [loadMessages]);
  useEffect(() => { loadLogs(); }, [loadLogs]);
  useEffect(() => { loadHistory(); }, [loadHistory]);

  if (!user) {
    return (
      <div>
        <button style={styles.back} onClick={() => navigate('/users')}>← Все пользователи</button>
        {msg ? <div style={{ color: 'var(--error)' }}>{msg}</div> : <div style={{ color: 'var(--text-secondary)' }}>Загрузка...</div>}
      </div>
    );
  }

  const saveAttitude = async (s: number, rt: string) => {
    if (!user_id) return;
    setSaving(true);
    try {
      const u = await updateUser(user_id, { sentiment: s, relationship_type: rt });
      setUser(u);
      loadHistory();
      setMsg(`Отношение обновлено: ${u.closeness_label} (${u.sentiment.toFixed(2)})`);
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const saveProfile = async () => {
    if (!user_id) return;
    setSaving(true);
    try {
      const u = await updateUser(user_id, {
        preferred_name: preferredName,
        user_name: userName,
        appearance_description: appearance,
      });
      setUser(u);
      setMsg('Профиль обновлён');
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const handleAddMemory = async () => {
    if (!user_id || !newMem.trim()) return;
    try {
      await createUserMemory(user_id, { content: newMem });
      setNewMem('');
      setMemPage(1);
      loadMemories();
      loadUser();
      setMsg('Память добавлена');
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleDeleteMemory = async (id: string) => {
    if (!confirm('Удалить эту память?')) return;
    try {
      await deleteMemory(id);
      loadMemories();
      loadUser();
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleSaveMemoryEdit = async () => {
    if (!editMemId) return;
    try {
      await updateMemory(editMemId, { content: editMemText });
      setEditMemId(null);
      loadMemories();
      setMsg('Память обновлена');
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handlePortrait = async () => {
    if (!user_id) return;
    setPortraitLoading(true);
    try {
      const r = await generatePortrait(user_id);
      setPortrait(r.portrait);
      setPortraitMeta(`${r.model}${r.timing ? ` · ${r.timing}` : ''}`);
      setMsg('');
    } catch (e: unknown) {
      setMsg(`Портрет не сгенерировался: ${e instanceof Error ? e.message : e}`);
    } finally {
      setPortraitLoading(false);
    }
  };

  const initial = (user.display_name || '?').slice(0, 1).toUpperCase();
  const memPages = Math.max(1, Math.ceil(memTotal / 10));
  const msgPages = Math.max(1, Math.ceil(msgTotal / 10));

  return (
    <div style={styles.page}>
      <button style={styles.back} onClick={() => navigate('/users')}>← Все пользователи</button>

      <div style={styles.header}>
        <div style={styles.avatar}>{initial}</div>
        <div>
          <div style={styles.title}>{user.display_name}</div>
          <div style={styles.sub}>{user.user_id}</div>
        </div>
        <span style={styles.badge}>{user.relationship_type}</span>
        <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {user.closeness_label} ({user.sentiment.toFixed(2)})
        </span>
      </div>

      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.stats}>
        <div style={styles.stat}><div style={styles.statNum}>{user.messages_count}</div><div style={styles.statLabel}>сообщений</div></div>
        <div style={styles.stat}><div style={styles.statNum}>{user.memories_count}</div><div style={styles.statLabel}>воспоминаний</div></div>
        <div style={styles.stat}><div style={styles.statNum}>{user.chats_count}</div><div style={styles.statLabel}>чатов</div></div>
        <div style={styles.stat}>
          <div style={{ ...styles.statNum, fontSize: '14px', paddingTop: '6px' }}>
            {user.last_activity ? user.last_activity.slice(0, 16).replace('T', ' ') : '—'}
          </div>
          <div style={styles.statLabel}>активность</div>
        </div>
      </div>

      <div style={styles.grid}>
        {/* ── Attitude ── */}
        <div style={styles.card}>
          <div style={styles.cardTitle}>Отношение Милы</div>
          <div style={styles.label}>Быстрая смена</div>
          <div style={styles.presetRow}>
            {SENTIMENT_PRESETS.map(p => (
              <button
                key={p.label}
                style={{
                  ...styles.btnSecondary,
                  ...(Math.abs(sentiment - p.value) < 0.01
                    ? { borderColor: 'var(--accent)', color: 'var(--accent)' }
                    : {}),
                }}
                disabled={saving}
                onClick={() => { setSentiment(p.value); saveAttitude(p.value, relType); }}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div style={styles.field}>
            <label style={styles.label}>
              Тонкая настройка: {closenessLabel(sentiment)} ({sentiment.toFixed(2)})
            </label>
            <input
              type="range"
              min={-1}
              max={1}
              step={0.05}
              value={sentiment}
              onChange={e => setSentiment(parseFloat(e.target.value))}
              style={styles.slider}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: 'var(--text-secondary)' }}>
              <span>−1 враждебность</span><span>+1 близость</span>
            </div>
          </div>
          <div style={styles.field}>
            <label style={styles.label}>Тип связи</label>
            <select style={styles.input} value={relType} onChange={e => setRelType(e.target.value)}>
              {RELATIONSHIP_OPTIONS.map(o => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>
          <button style={styles.btn} disabled={saving} onClick={() => saveAttitude(sentiment, relType)}>
            Сохранить отношение
          </button>
          <AttitudeChart points={history} />
        </div>

        {/* ── Profile ── */}
        <div style={styles.card}>
          <div style={styles.cardTitle}>Профиль</div>
          <div style={styles.field}>
            <label style={styles.label}>Отображаемое имя (preferred_name)</label>
            <input style={styles.input} value={preferredName} onChange={e => setPreferredName(e.target.value)} placeholder="Как Мила называет пользователя" />
          </div>
          <div style={styles.field}>
            <label style={styles.label}>Имя из Telegram (user_name)</label>
            <input style={styles.input} value={userName} onChange={e => setUserName(e.target.value)} />
          </div>
          <div style={styles.field}>
            <label style={styles.label}>Описание внешности / заметки</label>
            <textarea style={styles.textarea} value={appearance} onChange={e => setAppearance(e.target.value)} placeholder="Что Мила знает о внешности..." />
          </div>
          <button style={styles.btn} disabled={saving} onClick={saveProfile}>Сохранить профиль</button>
        </div>
      </div>

      {/* ── Portrait ── */}
      <div style={styles.cardFull}>
        <div style={styles.cardTitle}>Что Мила думает о пользователе</div>
        {!portrait && !portraitLoading && (
          <div>
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
              LLM-сводка по всей памяти, статистике и недавним сообщениям.
            </div>
            <button style={styles.btn} onClick={handlePortrait}>Сгенерировать портрет</button>
          </div>
        )}
        {portraitLoading && (
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Мила думает...</div>
        )}
        {portrait && (
          <div>
            <div style={{ fontSize: '14px', lineHeight: '1.6', whiteSpace: 'pre-wrap', color: 'var(--text-primary)' }}>
              {portrait}
            </div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '12px' }}>
              <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>{portraitMeta}</span>
              <span style={{ flex: 1 }} />
              <button style={styles.btnSecondary} disabled={portraitLoading} onClick={handlePortrait}>
                Обновить
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── Memory ── */}
      <div style={styles.cardFull}>
        <div style={styles.cardTitle}>Память о пользователе ({memTotal})</div>
        <div style={styles.toolbar}>
          <input
            style={{ ...styles.input, flex: 1 }}
            placeholder="Новая память..."
            value={newMem}
            onChange={e => setNewMem(e.target.value)}
          />
          <button style={styles.btn} onClick={handleAddMemory}>Добавить</button>
        </div>
        <div style={styles.toolbar}>
          <input
            style={{ ...styles.input, flex: 1 }}
            placeholder="Поиск по памяти..."
            value={memSearch}
            onChange={e => { setMemSearch(e.target.value); setMemPage(1); }}
          />
        </div>
        {memories.length === 0 && (
          <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>Памяти пока нет.</div>
        )}
        {memories.map(m => (
          <div key={m.mem_id} style={styles.memItem}>
            {editMemId === m.mem_id ? (
              <div>
                <textarea style={styles.textarea} value={editMemText} onChange={e => setEditMemText(e.target.value)} />
                <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
                  <button style={styles.btn} onClick={handleSaveMemoryEdit}>Сохранить</button>
                  <button style={styles.btnSecondary} onClick={() => setEditMemId(null)}>Отмена</button>
                </div>
              </div>
            ) : (
              <div>
                <div>{m.content}</div>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '6px' }}>
                  <span style={styles.badge}>{m.type}</span>
                  <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                    {m.created_at ? m.created_at.slice(0, 16).replace('T', ' ') : ''}
                  </span>
                  <span style={{ flex: 1 }} />
                  <button style={styles.btnSecondary} onClick={() => { setEditMemId(m.mem_id); setEditMemText(m.content); }}>Ред.</button>
                  <button style={styles.btnDanger} onClick={() => handleDeleteMemory(m.mem_id)}>Удалить</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {memPages > 1 && (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '8px' }}>
            <button style={styles.btnSecondary} disabled={memPage <= 1} onClick={() => setMemPage(memPage - 1)}>Назад</button>
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Стр. {memPage} из {memPages}</span>
            <button style={styles.btnSecondary} disabled={memPage >= memPages} onClick={() => setMemPage(memPage + 1)}>Вперёд</button>
          </div>
        )}
      </div>

      {/* ── Dialog history ── */}
      <div style={styles.cardFull}>
        <div style={styles.cardTitle}>История диалогов ({msgTotal})</div>
        {user.chats.length > 0 && (
          <div style={{ marginBottom: '12px', fontSize: '13px', color: 'var(--text-secondary)' }}>
            Чаты: {user.chats.map(c => `${c.chat_name || c.chat_id} (${c.messages_count})`).join(' · ')}
          </div>
        )}
        {messages.length === 0 && (
          <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>Сообщений пока нет.</div>
        )}
        {messages.map(m => (
          <div key={m.chunk_id} style={styles.msgItem}>
            <div style={styles.msgMeta}>
              {m.chat_name || m.chat_id} · {m.time_start ? m.time_start.slice(0, 16).replace('T', ' ') : ''} · [{m.kind}]
            </div>
            <div style={styles.msgBody}>{m.content}</div>
          </div>
        ))}
        {msgPages > 1 && (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '8px' }}>
            <button style={styles.btnSecondary} disabled={msgPage <= 1} onClick={() => setMsgPage(msgPage - 1)}>Назад</button>
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Стр. {msgPage} из {msgPages}</span>
            <button style={styles.btnSecondary} disabled={msgPage >= msgPages} onClick={() => setMsgPage(msgPage + 1)}>Вперёд</button>
          </div>
        )}
      </div>

      {/* ── Pipeline traces ── */}
      {logs.length > 0 && (
        <div style={styles.cardFull}>
          <div style={styles.cardTitle}>Последние разборы Pipeline</div>
          {logs.map(l => (
            <button key={l.filename} style={styles.logItem} onClick={() => navigate(`/pipeline?log=${encodeURIComponent(l.filename)}`)}>
              <div style={{ fontWeight: 600 }}>{l.timestamp.slice(0, 16).replace('T', ' ')}</div>
              <div style={{ color: 'var(--text-secondary)', fontSize: '12px', marginTop: '4px' }}>
                {(l.user_message || '').slice(0, 140)}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
