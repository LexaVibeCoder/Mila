import React, { useEffect, useState, useCallback } from 'react';
import {
  getStateAgentStatus,
  runStateAgent,
  overrideNeeds,
  triggerProactive,
  getTriggerState,
  StateAgentStatus,
  ProactiveTriggerState,
} from '../api/client';

const styles: Record<string, any> = {
  page: { maxWidth: '900px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  section: {
    background: 'var(--bg-card)', borderRadius: '12px', padding: '20px',
    border: '1px solid var(--border)', marginBottom: '16px',
  },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  label: { fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '4px' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px', marginBottom: '16px' },
  card: { background: 'var(--bg-primary)', borderRadius: '8px', padding: '16px', border: '1px solid var(--border)' },
  cardTitle: { fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase' as const, letterSpacing: '1px', marginBottom: '8px' },
  cardValue: { fontSize: '22px', fontWeight: 700, color: 'var(--text-primary)' },
  meterBar: {
    width: '100%', height: '8px', background: 'var(--border)', borderRadius: '4px',
    overflow: 'hidden', marginTop: '6px',
  },
  meterFill: (val: number, color: string) => ({
    height: '100%', width: `${val * 100}%`, background: color, borderRadius: '4px',
    transition: 'width 0.5s ease',
  }),
  row: { display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' as const },
  btn: {
    padding: '10px 24px', borderRadius: '8px', border: 'none', fontSize: '14px',
    fontWeight: 600, cursor: 'pointer', transition: 'all 0.2s',
  },
  btnPrimary: { background: 'var(--accent)', color: '#fff' },
  btnOutline: { background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent)' },
  btnDisabled: { background: 'var(--border)', color: 'var(--text-secondary)', cursor: 'not-allowed' },
  badge: {
    display: 'inline-block', padding: '4px 12px', borderRadius: '12px',
    fontSize: '12px', fontWeight: 600,
  },
  badgeActive: { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' },
  badgeWarning: { background: 'rgba(255,159,67,0.2)', color: 'var(--warning)' },
  badgeDanger: { background: 'rgba(214,48,49,0.2)', color: 'var(--error)' },
  divider: { height: '1px', background: 'var(--border)', margin: '16px 0' },
  sliderRow: { marginBottom: '12px' },
  sliderHeader: { display: 'flex', justifyContent: 'space-between', marginBottom: '4px' },
  slider: {
    width: '100%', height: '6px', borderRadius: '3px',
    background: 'var(--border)', outline: 'none',
    WebkitAppearance: 'none' as const, appearance: 'none' as const,
    cursor: 'pointer',
  },
  probRow: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' },
  probLabel: { width: '100px', fontSize: '13px', color: 'var(--text-secondary)' },
  probBar: {
    flex: 1, height: '8px', background: 'var(--border)', borderRadius: '4px', overflow: 'hidden',
  },
  probFill: (val: number) => ({
    height: '100%', width: `${val * 100}%`, background: 'var(--accent)', borderRadius: '4px',
    transition: 'width 0.5s ease',
  }),
  probValue: { width: '50px', textAlign: 'right' as const, fontSize: '13px', color: 'var(--text-primary)', fontWeight: 600 },
  actionBadge: {
    display: 'inline-flex', alignItems: 'center', gap: '6px',
    padding: '8px 20px', borderRadius: '8px', fontSize: '16px', fontWeight: 700,
  },
};

const ACTION_INFO: Record<string, { label: string; color: string; bg: string }> = {
  wait: { label: '⏳ Wait', color: 'var(--text-secondary)', bg: 'rgba(255,255,255,0.05)' },
  sleep: { label: '💤 Sleep', color: 'var(--warning)', bg: 'rgba(255,159,67,0.15)' },
  write_to_user: { label: '✍️ Write to User', color: 'var(--success)', bg: 'rgba(0,184,148,0.15)' },
  plan_day: { label: '📋 Plan Day', color: 'var(--accent)', bg: 'rgba(0,123,255,0.15)' },
};

const NEED_COLORS: Record<string, string> = {
  fatigue: '#ff6b6b',
  need_social: '#48dbfb',
  boredom: '#ff9ff3',
};

function formatTime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

export default function StateAgentControl() {
  const [status, setStatus] = useState<StateAgentStatus | null>(null);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [loading, setLoading] = useState(false);

  const [overrideFatigue, setOverrideFatigue] = useState<number | null>(null);
  const [overrideSocial, setOverrideSocial] = useState<number | null>(null);
  const [overrideBoredom, setOverrideBoredom] = useState<number | null>(null);
  const [overrideSeconds, setOverrideSeconds] = useState<string>('');
  const [overrideHour, setOverrideHour] = useState<string>('');
  const [overrideDialog, setOverrideDialog] = useState<boolean | null>(null);
  const [useOverrides, setUseOverrides] = useState(false);

  const [forceAction, setForceAction] = useState<string>('auto');
  const [triggerState, setTriggerState] = useState<ProactiveTriggerState | null>(null);
  const [triggerLoading, setTriggerLoading] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const s = await getStateAgentStatus();
      setStatus(s);
    } catch {
      // silent
    }
  }, []);

  const fetchTrigger = useCallback(async () => {
    try {
      const t = await getTriggerState();
      setTriggerState(t);
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    fetchTrigger();
    const interval = setInterval(() => { fetchStatus(); fetchTrigger(); }, 5000);
    return () => clearInterval(interval);
  }, [fetchStatus, fetchTrigger]);

  const showSuccess = (msg: string) => {
    setSuccessMsg(msg);
    setTimeout(() => setSuccessMsg(''), 4000);
  };

  const buildOverrideData = (): Record<string, unknown> | null => {
    if (!useOverrides) return null;
    const data: Record<string, unknown> = {};
    if (overrideFatigue !== null) data.fatigue = overrideFatigue;
    if (overrideSocial !== null) data.need_social = overrideSocial;
    if (overrideBoredom !== null) data.boredom = overrideBoredom;
    if (overrideSeconds) data.seconds_since_last_msg = parseFloat(overrideSeconds);
    if (overrideHour) data.current_hour = parseInt(overrideHour);
    if (overrideDialog !== null) data.in_active_dialog = overrideDialog;
    return data;
  };

  const handleTrigger = async (dryRun: boolean) => {
    try {
      setError('');
      setTriggerLoading(true);
      const t = await triggerProactive({
        dry_run: dryRun,
        force_action: forceAction === 'auto' ? null : forceAction,
        needs_override: buildOverrideData(),
      });
      setTriggerState(t);
      showSuccess(dryRun ? 'Dry-run queued — bot picks it up within ~10s' : 'Trigger queued — bot picks it up within ~10s');
      await fetchTrigger();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to trigger');
    } finally {
      setTriggerLoading(false);
    }
  };

  const handleRunInference = async () => {
    try {
      setError('');
      setLoading(true);
      if (useOverrides) {
        const data = buildOverrideData() || {};
        const result = await overrideNeeds(data);
        setStatus(result);
      } else {
        const result = await runStateAgent();
        setStatus(prev => prev ? { ...prev, inference: result } : prev);
      }
      showSuccess('Inference completed');
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to run inference');
    } finally {
      setLoading(false);
    }
  };

  const isModelLoaded = status?.model?.loaded;
  const needs = status?.needs;
  const inference = status?.inference;

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>🧠 State Agent</h1>

      {/* Messages */}
      {error && (
        <div style={{ color: 'var(--error)', marginBottom: '16px', fontSize: '13px', padding: '10px 16px', background: 'rgba(214,48,49,0.1)', borderRadius: '8px' }}>
          {error}
        </div>
      )}
      {successMsg && (
        <div style={{ color: 'var(--success)', marginBottom: '16px', fontSize: '13px', padding: '10px 16px', background: 'rgba(0,184,148,0.1)', borderRadius: '8px' }}>
          {successMsg}
        </div>
      )}

      {/* ── Model Info ─────────────────────────────────────── */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>🤖 Model</h2>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '13px' }}>
          <div style={{ color: 'var(--text-secondary)' }}>Status:</div>
          <div>
            <span style={{ ...styles.badge, ...(isModelLoaded ? styles.badgeActive : styles.badgeDanger) }}>
              {isModelLoaded ? '✓ Loaded' : '✗ Not Loaded'}
            </span>
          </div>
          <div style={{ color: 'var(--text-secondary)' }}>Actions:</div>
          <div style={{ color: 'var(--text-primary)' }}>{(status?.model?.actions || []).join(', ')}</div>
          <div style={{ color: 'var(--text-secondary)' }}>Features:</div>
          <div style={{ color: 'var(--text-primary)' }}>{status?.model?.num_features ?? '-'}</div>
          {status?.model?.error && (
            <>
              <div style={{ color: 'var(--text-secondary)' }}>Error:</div>
              <div style={{ color: 'var(--error)', fontSize: '12px' }}>{status.model.error}</div>
            </>
          )}
        </div>
      </div>

      {/* ── Current Needs ──────────────────────────────────── */}
      {needs && (
        <div style={styles.section}>
          <h2 style={styles.sectionTitle}>📊 Current Needs</h2>
          <div style={styles.grid}>
            {/* Seconds since last msg */}
            <div style={styles.card}>
              <div style={styles.cardTitle}>Since Last Message</div>
              <div style={{ ...styles.cardValue, fontSize: '18px' }}>{formatTime(needs.seconds_since_last_msg)}</div>
              <div style={styles.label}>{needs.in_active_dialog ? '💬 Active dialog' : '🔇 No active dialog'}</div>
            </div>

            {/* Current hour */}
            <div style={styles.card}>
              <div style={styles.cardTitle}>Current Hour</div>
              <div style={styles.cardValue}>{needs.current_hour}:00</div>
              <div style={styles.label}>{needs.current_hour >= 6 && needs.current_hour < 12 ? '🌅 Morning' : needs.current_hour >= 12 && needs.current_hour < 18 ? '☀️ Day' : needs.current_hour >= 18 && needs.current_hour < 22 ? '🌆 Evening' : '🌙 Night'}</div>
            </div>

            {/* Timestamp */}
            <div style={styles.card}>
              <div style={styles.cardTitle}>Last Updated</div>
              <div style={{ ...styles.cardValue, fontSize: '14px' }}>{new Date(needs.timestamp).toLocaleTimeString()}</div>
              <div style={styles.label}>{new Date(needs.timestamp).toLocaleDateString()}</div>
            </div>
          </div>

          {/* Need meters */}
          <div style={{ marginTop: '8px' }}>
            {[
              { key: 'fatigue', label: 'Fatigue', value: needs.fatigue, color: NEED_COLORS.fatigue },
              { key: 'need_social', label: 'Need Social', value: needs.need_social, color: NEED_COLORS.need_social },
              { key: 'boredom', label: 'Boredom', value: needs.boredom, color: NEED_COLORS.boredom },
            ].map(n => (
              <div key={n.key} style={{ marginBottom: '12px' }}>
                <div style={styles.sliderHeader}>
                  <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{n.label}</span>
                  <span style={{ fontSize: '13px', fontWeight: 600, color: n.color }}>{(n.value * 100).toFixed(0)}%</span>
                </div>
                <div style={styles.meterBar}>
                  <div style={styles.meterFill(n.value, n.color)}></div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Inference Result ────────────────────────────────── */}
      {inference && (
        <div style={styles.section}>
          <h2 style={styles.sectionTitle}>🎯 Inference Result</h2>

          {/* Action badge */}
          <div style={{ textAlign: 'center', marginBottom: '20px' }}>
            <div style={{
              ...styles.actionBadge,
              color: ACTION_INFO[inference.action]?.color ?? 'var(--text-primary)',
              background: ACTION_INFO[inference.action]?.bg ?? 'var(--bg-primary)',
              border: `1px solid ${ACTION_INFO[inference.action]?.color ?? 'var(--border)'}`,
            }}>
              {ACTION_INFO[inference.action]?.label ?? inference.action}
            </div>
            {inference.fallback && (
              <div style={{ fontSize: '12px', color: 'var(--warning)', marginTop: '8px' }}>
                ⚠️ Using rule-based fallback (model not loaded)
              </div>
            )}
            {inference.model_loaded && (
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '8px' }}>
                Inference: {inference.inference_time_ms}ms
              </div>
            )}
          </div>

          {/* Probability bars */}
          <div style={{ marginTop: '8px' }}>
            <div style={styles.label}>Probabilities</div>
            {Object.entries(inference.probabilities).map(([action, prob]) => (
              <div key={action} style={styles.probRow}>
                <span style={styles.probLabel}>{ACTION_INFO[action]?.label ?? action}</span>
                <div style={styles.probBar}>
                  <div style={styles.probFill(prob)}></div>
                </div>
                <span style={styles.probValue}>{(prob * 100).toFixed(1)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Trigger & Simulate (real bot run, no 27-min wait) ── */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>⚡ Trigger &amp; Simulate</h2>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
          Запускает настоящий проактивный проход в боте (тот же код, что срабатывает после 27 мин тишины).
          Idle/кулдаун не проверяются, защита от сна и чужого диалога — действует.
          {useOverrides ? ' Входные needs — из слайдеров выше.' : ' Входные needs — живые данные бота.'}
        </div>

        <div style={{ ...styles.row, marginBottom: '12px' }}>
          <span style={styles.label}>Форсировать действие:</span>
          <select
            value={forceAction}
            onChange={e => setForceAction(e.target.value)}
            style={{
              padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)',
              background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px',
            }}
          >
            <option value="auto">Auto (модель решает)</option>
            <option value="write_to_user">✍️ write_to_user</option>
            <option value="sleep">💤 sleep</option>
            <option value="wait">⏳ wait</option>
            <option value="plan_day">📋 plan_day</option>
          </select>
        </div>

        <div style={styles.row}>
          <button
            style={{ ...styles.btn, ...styles.btnPrimary, ...(triggerLoading ? styles.btnDisabled : {}) }}
            disabled={triggerLoading}
            onClick={() => handleTrigger(false)}
          >
            {triggerLoading ? '⏳ Queued...' : '▶️ Trigger now'}
          </button>
          <button
            style={{ ...styles.btn, ...styles.btnOutline, ...(triggerLoading ? styles.btnDisabled : {}) }}
            disabled={triggerLoading}
            onClick={() => handleTrigger(true)}
          >
            🔍 Dry-run
          </button>
          <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            Dry-run безопасен: только описывает, что бы произошло.
          </span>
        </div>
        <div style={{ fontSize: '12px', color: 'var(--warning)', marginTop: '8px' }}>
          ⚠️ Trigger now — реальное действие: write_to_user напишет человеку, sleep усыпит на 8ч.
        </div>

        <div style={styles.divider}></div>

        {/* Trigger status */}
        {triggerState?.pending && (
          <div style={{ fontSize: '13px', color: 'var(--accent)', marginBottom: '8px' }}>
            ⏳ Команда в очереди — бот подхватит в течение ~10с...
          </div>
        )}
        {triggerState?.last_run ? (
          <div style={{ fontSize: '13px' }}>
            <div style={{ ...styles.row, marginBottom: '8px' }}>
              <span style={{
                ...styles.badge,
                ...(triggerState.last_run.dry_run ? styles.badgeWarning : styles.badgeActive),
              }}>
                {triggerState.last_run.dry_run ? '🔍 DRY-RUN' : '▶️ REAL'}
              </span>
              <span style={{
                ...styles.actionBadge, fontSize: '14px', padding: '4px 14px',
                color: ACTION_INFO[triggerState.last_run.action]?.color ?? 'var(--text-primary)',
                background: ACTION_INFO[triggerState.last_run.action]?.bg ?? 'var(--bg-primary)',
              }}>
                {ACTION_INFO[triggerState.last_run.action]?.label ?? triggerState.last_run.action}
              </span>
              <span style={{ color: 'var(--text-secondary)', fontSize: '12px' }}>
                {new Date(triggerState.last_run.at).toLocaleString()}
                {' • '}idle {formatTime(triggerState.last_run.idle_seconds)}
                {triggerState.last_run.forced ? ' • forced' : ' • scheduled'}
              </span>
            </div>
            <div style={{ color: 'var(--text-primary)' }}>{triggerState.last_run.result}</div>
          </div>
        ) : (
          !triggerState?.pending && (
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              Запусков пока не было.
            </div>
          )
        )}
      </div>

      {/* ── Controls ────────────────────────────────────────── */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>🎮 Controls</h2>

        {/* Use overrides toggle */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px',
          cursor: 'pointer', userSelect: 'none' as const,
        }} onClick={() => setUseOverrides(!useOverrides)}>
          <div style={{
            width: '44px', height: '24px', borderRadius: '12px',
            background: useOverrides ? 'var(--accent)' : 'var(--border)',
            position: 'relative' as const, transition: 'background 0.2s',
          }}>
            <div style={{
              width: '20px', height: '20px', borderRadius: '50%', background: '#fff',
              position: 'absolute' as const, top: '2px',
              left: useOverrides ? '22px' : '2px',
              transition: 'left 0.2s', boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
            }} />
          </div>
          <div>
            <div style={{ fontSize: '14px', color: 'var(--text-primary)', fontWeight: 500 }}>
              {useOverrides ? 'Manual Override Active' : 'Auto — real data from bot'}
            </div>
          </div>
        </div>

        {useOverrides && (
          <React.Fragment>
            <div style={styles.divider}></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
              {/* Fatigue slider */}
              <div style={styles.sliderRow}>
                <div style={styles.sliderHeader}>
                  <span style={{ color: NEED_COLORS.fatigue, fontWeight: 600 }}>Fatigue</span>
                  <span style={{ color: 'var(--text-primary)' }}>{(overrideFatigue ?? needs?.fatigue ?? 0.5) * 100}%</span>
                </div>
                <input
                  type="range" min="0" max="1" step="0.01"
                  value={overrideFatigue ?? needs?.fatigue ?? 0.5}
                  onChange={e => setOverrideFatigue(parseFloat(e.target.value))}
                  style={{
                    ...styles.slider,
                    accentColor: NEED_COLORS.fatigue,
                  }}
                />
              </div>

              {/* Need Social slider */}
              <div style={styles.sliderRow}>
                <div style={styles.sliderHeader}>
                  <span style={{ color: NEED_COLORS.need_social, fontWeight: 600 }}>Need Social</span>
                  <span style={{ color: 'var(--text-primary)' }}>{(overrideSocial ?? needs?.need_social ?? 0.5) * 100}%</span>
                </div>
                <input
                  type="range" min="0" max="1" step="0.01"
                  value={overrideSocial ?? needs?.need_social ?? 0.5}
                  onChange={e => setOverrideSocial(parseFloat(e.target.value))}
                  style={{
                    ...styles.slider,
                    accentColor: NEED_COLORS.need_social,
                  }}
                />
              </div>

              {/* Boredom slider */}
              <div style={styles.sliderRow}>
                <div style={styles.sliderHeader}>
                  <span style={{ color: NEED_COLORS.boredom, fontWeight: 600 }}>Boredom</span>
                  <span style={{ color: 'var(--text-primary)' }}>{(overrideBoredom ?? needs?.boredom ?? 0.5) * 100}%</span>
                </div>
                <input
                  type="range" min="0" max="1" step="0.01"
                  value={overrideBoredom ?? needs?.boredom ?? 0.5}
                  onChange={e => setOverrideBoredom(parseFloat(e.target.value))}
                  style={{
                    ...styles.slider,
                    accentColor: NEED_COLORS.boredom,
                  }}
                />
              </div>

              {/* Seconds since last msg */}
              <div>
                <div style={styles.label}>Seconds since last message</div>
                <input
                  style={{
                    width: '100%', padding: '8px 12px', borderRadius: '6px',
                    border: '1px solid var(--border)', background: 'var(--bg-primary)',
                    color: 'var(--text-primary)', fontSize: '14px',
                    boxSizing: 'border-box' as const,
                  }}
                  type="number" min="0"
                  value={overrideSeconds || String(needs?.seconds_since_last_msg?.toFixed(0) ?? '0')}
                  onChange={e => setOverrideSeconds(e.target.value)}
                  placeholder="Seconds"
                />
              </div>

              {/* Hour override */}
              <div>
                <div style={styles.label}>Current hour (0-23)</div>
                <input
                  style={{
                    width: '100%', padding: '8px 12px', borderRadius: '6px',
                    border: '1px solid var(--border)', background: 'var(--bg-primary)',
                    color: 'var(--text-primary)', fontSize: '14px',
                    boxSizing: 'border-box' as const,
                  }}
                  type="number" min="0" max="23"
                  value={overrideHour || String(needs?.current_hour ?? '')}
                  onChange={e => setOverrideHour(e.target.value)}
                  placeholder="Hour"
                />
              </div>

              {/* Active dialog toggle */}
              <div>
                <div style={styles.label}>In active dialog</div>
                <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                  <button
                    style={{
                      ...styles.btn, padding: '6px 16px', fontSize: '13px',
                      ...(overrideDialog === true || (overrideDialog === null && needs?.in_active_dialog) ? styles.btnPrimary : styles.btnOutline),
                    }}
                    onClick={() => setOverrideDialog(true)}
                  >Yes</button>
                  <button
                    style={{
                      ...styles.btn, padding: '6px 16px', fontSize: '13px',
                      ...(overrideDialog === false || (overrideDialog === null && !needs?.in_active_dialog) ? styles.btnPrimary : styles.btnOutline),
                    }}
                    onClick={() => setOverrideDialog(false)}
                  >No</button>
                  {overrideDialog !== null && (
                    <button
                      style={{ ...styles.btn, padding: '6px 16px', fontSize: '13px', ...styles.btnOutline }}
                      onClick={() => setOverrideDialog(null)}
                    >Auto</button>
                  )}
                </div>
              </div>
            </div>
          </React.Fragment>
        )}

        <div style={styles.divider}></div>

        {/* Run button */}
        <div style={styles.row}>
          <button
            style={{ ...styles.btn, ...styles.btnPrimary, ...(loading ? styles.btnDisabled : {}) }}
            disabled={loading}
            onClick={handleRunInference}
          >
            {loading ? '⏳ Running...' : '▶️ Run Inference'}
          </button>
          <button
            style={{ ...styles.btn, ...styles.btnOutline }}
            onClick={fetchStatus}
          >
            🔄 Refresh
          </button>
          <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            {useOverrides ? 'Using manual overrides' : 'Using real data from bot + DB'}
          </span>
        </div>
      </div>

      {!needs && (
        <div style={{ color: 'var(--text-secondary)', padding: '40px', textAlign: 'center' }}>
          Loading state agent data...
        </div>
      )}
    </div>
  );
}
