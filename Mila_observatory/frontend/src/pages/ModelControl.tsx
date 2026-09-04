import React, { useEffect, useState } from 'react';
import { getModelConfig, updateMainModel, updateNightModel, testModel, ModelData, TestQueryResult } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '900px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  section: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)', marginBottom: '16px' },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  field: { marginBottom: '12px' },
  label: { display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' },
  input: { width: '100%', padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px' },
  textarea: { width: '100%', padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px', minHeight: '80px', fontFamily: 'monospace' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  resultBox: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '12px', marginTop: '12px', fontSize: '13px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', maxHeight: '300px', overflow: 'auto' },
  row: { display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '12px' },
  toggleRow: { display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '12px', marginTop: '4px' },
  toggle: { position: 'relative', width: '44px', height: '24px', borderRadius: '12px', border: '1px solid var(--border)', cursor: 'pointer', transition: 'background 0.15s', flexShrink: 0 },
  toggleHint: { fontSize: '12px', color: 'var(--text-secondary)' },
};

export default function ModelControl() {
  const [config, setConfig] = useState<ModelData | null>(null);
  const [mainForm, setMainForm] = useState<Record<string, string>>({});
  const [nightForm, setNightForm] = useState<Record<string, string>>({});
  const [testPrompt, setTestPrompt] = useState('Say hello and introduce yourself briefly.');
  const [testResult, setTestResult] = useState<TestQueryResult | null>(null);
  const [testLoading, setTestLoading] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    getModelConfig().then(d => {
      setConfig(d);
      const m = (d.main || {}) as Record<string, unknown>;
      const n = (d.night || {}) as Record<string, unknown>;
      setMainForm({
        model: String(m.model || ''),
        api_base: String(m.api_base || ''),
        temperature: String(m.temperature ?? ''),
        max_tokens: String(m.max_tokens ?? ''),
        tool_choice: String(m.tool_choice || 'required'),
      });
      setNightForm({
        model: String(n.model || ''),
        api_base: String(n.api_base || ''),
        temperature: String(n.temperature ?? ''),
        max_tokens: String(n.max_tokens ?? ''),
        tool_choice: String(n.tool_choice || 'required'),
      });
    });
  }, []);

  const saveMain = async () => {
    try {
      const params: Record<string, unknown> = {};
      if (mainForm.model) params.model = mainForm.model;
      if (mainForm.api_base) params.api_base = mainForm.api_base;
      if (mainForm.temperature) params.temperature = parseFloat(mainForm.temperature);
      if (mainForm.max_tokens) params.max_tokens = parseInt(mainForm.max_tokens);
      if (mainForm.tool_choice) params.tool_choice = mainForm.tool_choice;
      await updateMainModel(params);
      setMsg('Main LLM updated');
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const saveNight = async () => {
    try {
      const params: Record<string, unknown> = {};
      if (nightForm.model) params.model = nightForm.model;
      if (nightForm.api_base) params.api_base = nightForm.api_base;
      if (nightForm.temperature) params.temperature = parseFloat(nightForm.temperature);
      if (nightForm.max_tokens) params.max_tokens = parseInt(nightForm.max_tokens);
      if (nightForm.tool_choice) params.tool_choice = nightForm.tool_choice;
      await updateNightModel(params);
      setMsg('Night LLM updated');
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const runTest = async () => {
    setTestLoading(true);
    setTestResult(null);
    try {
      const r = await testModel(testPrompt, 'main');
      setTestResult(r);
    } catch (e: unknown) { setMsg(`Test error: ${e instanceof Error ? e.message : e}`); }
    setTestLoading(false);
  };

  const renderToolChoiceToggle = (
    value: string,
    onChange: (v: string) => void,
  ) => {
    const isRequired = (value || 'required') === 'required';
    return (
      <div style={styles.toggleRow}>
        <div
          style={{
            ...styles.toggle,
            background: isRequired ? 'var(--accent)' : 'var(--bg-secondary)',
          }}
          onClick={() => onChange(isRequired ? 'auto' : 'required')}
          title={isRequired ? 'tool_choice=required — модель обязана вызвать think()' : 'tool_choice=auto — модель сама решает вызывать ли think()'}
        >
          <div style={{
            position: 'absolute',
            top: '2px',
            left: isRequired ? '22px' : '2px',
            width: '18px',
            height: '18px',
            borderRadius: '50%',
            background: '#fff',
            transition: 'left 0.15s',
          }} />
        </div>
        <div>
          <div style={{ fontSize: '13px', color: 'var(--text-primary)', fontWeight: 600 }}>
            Принудительный вызов think() (tool_choice={isRequired ? 'required' : 'auto'})
          </div>
          <div style={styles.toggleHint}>
            {isRequired
              ? 'Вкл: модель обязана вызвать think(). Некоторые модели падают с ошибкой — тогда выключите.'
              : 'Выкл (auto): модель сама решает. Если перестала вызывать think() — включите обратно.'}
          </div>
        </div>
      </div>
    );
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Model Control</h1>
      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Main LLM (Daytime)</h2>
        <div style={styles.field}>
          <label style={styles.label}>Model</label>
          <input style={styles.input} value={mainForm.model || ''} onChange={e => setMainForm({...mainForm, model: e.target.value})} />
        </div>
        <div style={styles.field}>
          <label style={styles.label}>API Base</label>
          <input style={styles.input} value={mainForm.api_base || ''} onChange={e => setMainForm({...mainForm, api_base: e.target.value})} />
        </div>
        <div style={{ ...styles.row, gap: '12px' }}>
          <div style={{ flex: 1 }}>
            <label style={styles.label}>Temperature</label>
            <input style={styles.input} type="number" step="0.1" min="0" max="2" value={mainForm.temperature || ''} onChange={e => setMainForm({...mainForm, temperature: e.target.value})} />
          </div>
          <div style={{ flex: 1 }}>
            <label style={styles.label}>Max Tokens</label>
            <input style={styles.input} type="number" step="100" min="100" max="32000" value={mainForm.max_tokens || ''} onChange={e => setMainForm({...mainForm, max_tokens: e.target.value})} />
          </div>
        </div>
        {renderToolChoiceToggle(mainForm.tool_choice || 'required', v => setMainForm({...mainForm, tool_choice: v}))}
        <button style={styles.btn} onClick={saveMain}>Apply Changes</button>
      </div>

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Night LLM (Memory Worker)</h2>
        <div style={styles.field}>
          <label style={styles.label}>Model</label>
          <input style={styles.input} value={nightForm.model || ''} onChange={e => setNightForm({...nightForm, model: e.target.value})} />
        </div>
        <div style={styles.field}>
          <label style={styles.label}>API Base</label>
          <input style={styles.input} value={nightForm.api_base || ''} onChange={e => setNightForm({...nightForm, api_base: e.target.value})} />
        </div>
        <div style={{ ...styles.row, gap: '12px' }}>
          <div style={{ flex: 1 }}>
            <label style={styles.label}>Temperature</label>
            <input style={styles.input} type="number" step="0.1" min="0" max="2" value={nightForm.temperature || ''} onChange={e => setNightForm({...nightForm, temperature: e.target.value})} />
          </div>
          <div style={{ flex: 1 }}>
            <label style={styles.label}>Max Tokens</label>
            <input style={styles.input} type="number" step="100" min="100" max="32000" value={nightForm.max_tokens || ''} onChange={e => setNightForm({...nightForm, max_tokens: e.target.value})} />
          </div>
        </div>
        {renderToolChoiceToggle(nightForm.tool_choice || 'required', v => setNightForm({...nightForm, tool_choice: v}))}
        <button style={styles.btn} onClick={saveNight}>Apply Changes</button>
      </div>

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Model Playground</h2>
        <div style={styles.field}>
          <label style={styles.label}>Test Prompt</label>
          <textarea style={styles.textarea} value={testPrompt} onChange={e => setTestPrompt(e.target.value)} />
        </div>
        <button style={styles.btn} onClick={runTest} disabled={testLoading}>
          {testLoading ? 'Sending...' : 'Send Test Query'}
        </button>
        {testResult && (
          <div style={styles.resultBox}>
            <div style={{ color: 'var(--text-secondary)', marginBottom: '8px' }}>
              Model: {testResult.model} | Time: {testResult.timing} | Success: {String(testResult.success)}
            </div>
            {testResult.success ? testResult.response : <span style={{ color: 'var(--error)' }}>{testResult.error}</span>}
          </div>
        )}
      </div>
    </div>
  );
}