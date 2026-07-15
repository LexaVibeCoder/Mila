import React, { useEffect, useState } from 'react';
import { getTools, updateTool, ToolItem } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '700px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  list: { display: 'flex', flexDirection: 'column', gap: '8px' },
  card: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'var(--bg-card)', borderRadius: '10px', padding: '16px 20px', border: '1px solid var(--border)' },
  info: { flex: 1 },
  name: { fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)' },
  desc: { fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' },
  toggle: { width: '44px', height: '24px', borderRadius: '12px', border: 'none', cursor: 'pointer', position: 'relative', transition: 'background 0.2s' },
  toggleOn: { background: 'var(--success)' },
  toggleOff: { background: 'var(--bg-hover)' },
  toggleDot: { width: '18px', height: '18px', borderRadius: '50%', background: '#fff', position: 'absolute', top: '3px', transition: 'left 0.2s' },
};

export default function ToolsControl() {
  const [tools, setTools] = useState<ToolItem[]>([]);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    getTools().then(r => setTools(r.tools));
  }, []);

  const handleToggle = async (name: string, enabled: boolean) => {
    try {
      const updated = await updateTool(name, enabled);
      setTools(prev => prev.map(t => t.name === name ? updated : t));
      setMsg(`Tool '${name}' ${enabled ? 'enabled' : 'disabled'}`);
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Tools Control</h1>
      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.list}>
        {tools.map(tool => (
          <div key={tool.name} style={styles.card}>
            <div style={styles.info}>
              <div style={styles.name}>{tool.name}</div>
              <div style={styles.desc}>{tool.description}</div>
            </div>
            <button
              style={{ ...styles.toggle, ...(tool.enabled ? styles.toggleOn : styles.toggleOff) }}
              onClick={() => handleToggle(tool.name, !tool.enabled)}
            >
              <div style={{ ...styles.toggleDot, left: tool.enabled ? '23px' : '3px' }} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}