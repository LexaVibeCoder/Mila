import React, { useEffect, useState } from 'react';
import { getDashboard, DashboardData } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1200px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '16px', marginBottom: '24px' },
  card: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)' },
  cardTitle: { fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '8px' },
  cardValue: { fontSize: '28px', fontWeight: 700, color: 'var(--text-primary)' },
  cardSub: { fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px' },
  section: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)', marginBottom: '16px' },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  event: { padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: '13px' },
  eventTime: { color: 'var(--text-secondary)', fontSize: '11px' },
  eventDesc: { color: 'var(--text-primary)', marginTop: '2px' },
  badge: { display: 'inline-block', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 600 },
  badgeOnline: { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' },
  badgeOffline: { background: 'rgba(225,112,85,0.2)', color: 'var(--error)' },
};

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchData = async () => {
      try {
        const d = await getDashboard();
        setData(d);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : 'Failed to load');
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, []);

  if (error) return <div style={{ color: 'var(--error)' }}>Error: {error}</div>;
  if (!data) return <div style={{ color: 'var(--text-secondary)' }}>Loading...</div>;

  const { status, recent_events } = data;
  const uptimeStr = status.uptime
    ? `${Math.floor(status.uptime / 3600)}h ${Math.floor((status.uptime % 3600) / 60)}m`
    : 'N/A';

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Dashboard</h1>

      <div style={styles.grid}>
        <div style={styles.card}>
          <div style={styles.cardTitle}>Status</div>
          <div>
            <span style={{ ...styles.badge, ...(status.status === 'online' ? styles.badgeOnline : styles.badgeOffline) }}>
              {status.status}
            </span>
          </div>
          <div style={styles.cardSub}>Uptime: {uptimeStr}</div>
        </div>

        <div style={styles.card}>
          <div style={styles.cardTitle}>Model</div>
          <div style={{ ...styles.cardValue, fontSize: '20px' }}>{status.model || 'N/A'}</div>
          <div style={styles.cardSub}>Runtime config v{status.runtime_config_version}</div>
        </div>

        <div style={styles.card}>
          <div style={styles.cardTitle}>Messages</div>
          <div style={styles.cardValue}>{status.messages_total}</div>
          <div style={styles.cardSub}>Total chunks stored</div>
        </div>

        <div style={styles.card}>
          <div style={styles.cardTitle}>Memories</div>
          <div style={styles.cardValue}>{status.memories_total}</div>
          <div style={styles.cardSub}>Long-term facts</div>
        </div>
      </div>

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Recent Events</h2>
        {recent_events.length === 0 && (
          <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>No events yet</div>
        )}
        {recent_events.slice().reverse().map((ev, i) => (
          <div key={i} style={styles.event}>
            <div style={styles.eventTime}>{ev.timestamp}</div>
            <div style={styles.eventDesc}>
              <span style={{ color: 'var(--accent)' }}>{ev.type}</span>: {ev.description}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}