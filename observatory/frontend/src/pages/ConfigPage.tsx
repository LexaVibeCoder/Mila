import React, { useEffect, useState } from 'react';
import { getConfig, updateConfig, reloadConfig, getRuntimeState } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '900px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  section: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)', marginBottom: '16px' },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  pre: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '12px', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', overflow: 'auto', maxHeight: '400px', color: 'var(--text-primary)' },
  toolbar: { display: 'flex', gap: '8px', marginBottom: '16px' },
};

export default function ConfigPage() {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [msg, setMsg] = useState('');

  const load = () => {
    getRuntimeState().then(r => setConfig(r.config));
  };

  useEffect(() => { load(); }, []);

  const handleReload = async () => {
    try {
      const r = await reloadConfig();
      setMsg(r.message);
      load();
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Runtime State</h1>
      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.toolbar}>
        <button style={styles.btn} onClick={load}>Refresh</button>
        <button style={styles.btnSecondary} onClick={handleReload}>Reload from Disk</button>
      </div>

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Configuration JSON</h2>
        <pre style={styles.pre}>
          {config ? JSON.stringify(config, null, 2) : 'Loading...'}
        </pre>
      </div>
    </div>
  );
}