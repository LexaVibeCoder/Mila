import React, { useEffect, useState } from 'react';
import { getPersona, updatePersona, resetPersona, PersonaData } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '800px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  section: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)', marginBottom: '16px' },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  textarea: { width: '100%', minHeight: '300px', padding: '12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px', fontFamily: 'monospace', lineHeight: '1.5' },
  field: { marginBottom: '12px' },
  label: { display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' },
  input: { width: '100%', padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  row: { display: 'flex', gap: '12px', marginBottom: '8px', alignItems: 'center' },
  badge: { display: 'inline-block', padding: '2px 6px', borderRadius: '4px', fontSize: '11px', background: 'rgba(253,203,110,0.2)', color: 'var(--warning)' },
  paramBar: { height: '8px', borderRadius: '4px', flex: 1, background: 'var(--bg-secondary)', overflow: 'hidden' },
  paramFill: { height: '100%', borderRadius: '4px', background: 'var(--accent)', transition: 'width 0.3s' },
};

const paramLabels: Record<string, string> = {
  empathy: 'Эмпатия',
  humor: 'Юмор',
  formality: 'Формальность',
  creativity: 'Креативность',
};

export default function PersonalityEditor() {
  const [data, setData] = useState<PersonaData | null>(null);
  const [text, setText] = useState('');
  const [params, setParams] = useState<Record<string, number>>({});
  const [msg, setMsg] = useState('');

  useEffect(() => {
    getPersona().then(d => {
      setData(d);
      setText(d.custom_text);
      setParams(d.parameters);
    });
  }, []);

  const handleSave = async () => {
    try {
      await updatePersona({ custom_text: text, parameters: params });
      setMsg('Persona saved');
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const handleReset = async () => {
    if (!confirm('Reset persona to default?')) return;
    try {
      const d = await resetPersona();
      setData(d);
      setText(d.custom_text);
      setParams(d.parameters);
      setMsg('Persona reset to default');
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const handleParamChange = (key: string, value: number) => {
    setParams(prev => ({ ...prev, [key]: Math.max(0, Math.min(1, value)) }));
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Personality</h1>
      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      {data && (
        <div style={styles.section}>
          <h2 style={styles.sectionTitle}>Personality Parameters</h2>
          {Object.entries(params).map(([key, value]) => (
            <div key={key} style={styles.row}>
              <span style={{ width: '100px', fontSize: '13px', color: 'var(--text-primary)' }}>
                {paramLabels[key] || key}
              </span>
              <div style={styles.paramBar}>
                <div style={{ ...styles.paramFill, width: `${value * 100}%` }} />
              </div>
              <span style={{ width: '40px', textAlign: 'right', fontSize: '13px', color: 'var(--text-secondary)' }}>
                {Math.round(value * 100)}%
              </span>
              <input
                type="range" min="0" max="1" step="0.05"
                value={value}
                onChange={e => handleParamChange(key, parseFloat(e.target.value))}
                style={{ width: '100px' }}
              />
            </div>
          ))}
        </div>
      )}

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>
          Persona Text
          {data?.using_custom && <span style={{ ...styles.badge, marginLeft: '8px' }}>Custom</span>}
        </h2>
        <div style={styles.field}>
          <label style={styles.label}>
            {data?.using_custom
              ? 'Custom persona (empty = use default from persona.py)'
              : 'Default persona from persona.py. Edit to override.'}
          </label>
          <textarea
            style={styles.textarea}
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder="Enter custom persona text..."
          />
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button style={styles.btn} onClick={handleSave}>Save Persona</button>
          <button style={styles.btnSecondary} onClick={handleReset}>Reset to Default</button>
        </div>
      </div>
    </div>
  );
}