import React, { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { getUsers, UserItem } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1200px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '4px', color: 'var(--text-primary)' },
  subtitle: { fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '20px' },
  toolbar: { display: 'flex', gap: '12px', marginBottom: '16px', flexWrap: 'wrap' },
  input: { padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px', flex: 1, minWidth: '200px' },
  select: { padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px' },
  td: { padding: '10px 12px', borderBottom: '1px solid var(--border)', fontSize: '13px', verticalAlign: 'middle' },
  row: { cursor: 'pointer' },
  name: { fontWeight: 600, color: 'var(--text-primary)' },
  sub: { fontSize: '11px', color: 'var(--text-secondary)', fontFamily: 'monospace' },
  badge: { display: 'inline-block', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: 600 },
  num: { fontFamily: 'monospace', textAlign: 'right' as const },
  empty: { padding: '40px 20px', textAlign: 'center' as const, color: 'var(--text-secondary)', fontSize: '14px' },
};

function sentimentColor(s: number): string {
  if (s > 0.6) return '#00b894';
  if (s > 0.2) return '#6c5ce7';
  if (s < -0.6) return '#e17055';
  if (s < -0.2) return '#e17055';
  return 'var(--text-secondary)';
}

function sentimentBar(): React.CSSProperties {
  return {
    position: 'relative',
    width: '90px',
    height: '6px',
    background: 'var(--bg-hover)',
    borderRadius: '3px',
    overflow: 'hidden',
  };
}

function shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;
}

export default function UsersList() {
  const navigate = useNavigate();
  const [items, setItems] = useState<UserItem[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('last_activity');
  const [msg, setMsg] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await getUsers(search || undefined, sort);
      setItems(r.items);
      setTotal(r.total);
      setMsg('');
    } catch (e: unknown) {
      setMsg(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  }, [search, sort]);

  useEffect(() => {
    const t = setTimeout(load, search ? 300 : 0);
    return () => clearTimeout(t);
  }, [load]);

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Users</h1>
      <div style={styles.subtitle}>Все пользователи, с которыми общалась Мила — {total}</div>
      {msg && <div style={{ color: 'var(--error)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.toolbar}>
        <input
          style={styles.input}
          placeholder="Поиск по имени или user_id..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <select style={styles.select} value={sort} onChange={e => setSort(e.target.value)}>
          <option value="last_activity">Сначала активные</option>
          <option value="messages">По сообщениям</option>
          <option value="memories">По памяти</option>
          <option value="name">По имени</option>
        </select>
      </div>

      {loading ? (
        <div style={styles.empty}>Загрузка...</div>
      ) : items.length === 0 ? (
        <div style={styles.empty}>
          Пока никого нет. Пользователи появятся здесь после первых диалогов Милы.
        </div>
      ) : (
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Пользователь</th>
              <th style={styles.th}>Тип связи</th>
              <th style={styles.th}>Отношение</th>
              <th style={{ ...styles.th, textAlign: 'right' }}>Сообщ.</th>
              <th style={{ ...styles.th, textAlign: 'right' }}>Память</th>
              <th style={styles.th}>Активность</th>
            </tr>
          </thead>
          <tbody>
            {items.map(u => (
              <tr
                key={u.user_id}
                style={styles.row}
                onClick={() => navigate(`/users/${encodeURIComponent(u.user_id)}`)}
                onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-hover)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              >
                <td style={styles.td}>
                  <div style={styles.name}>{u.display_name}</div>
                  <div style={styles.sub}>{shortId(u.user_id)}</div>
                </td>
                <td style={styles.td}>
                  <span style={{ ...styles.badge, background: 'rgba(108,92,231,0.2)', color: 'var(--accent)' }}>
                    {u.relationship_type}
                  </span>
                </td>
                <td style={styles.td}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <div style={sentimentBar()}>
                      <div
                        style={{
                          position: 'absolute',
                          top: 0,
                          bottom: 0,
                          left: u.sentiment < 0 ? `${50 - Math.abs(u.sentiment) * 50}%` : '50%',
                          width: `${Math.abs(u.sentiment) * 50}%`,
                          background: sentimentColor(u.sentiment),
                          borderRadius: '3px',
                        }}
                      />
                      <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: '1px', background: 'var(--border)' }} />
                    </div>
                    <span style={{ fontSize: '12px', color: sentimentColor(u.sentiment), whiteSpace: 'nowrap' }}>
                      {u.closeness_label} ({u.sentiment.toFixed(2)})
                    </span>
                  </div>
                </td>
                <td style={{ ...styles.td, ...styles.num }}>{u.messages_count}</td>
                <td style={{ ...styles.td, ...styles.num }}>{u.memories_count}</td>
                <td style={{ ...styles.td, color: 'var(--text-secondary)', fontSize: '12px' }}>
                  {u.last_activity ? u.last_activity.slice(0, 16).replace('T', ' ') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
