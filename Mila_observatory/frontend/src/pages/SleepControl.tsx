import React, { useEffect, useState, useCallback } from 'react';
import {
  getSleepStatus,
  startSleep,
  wakeUp,
  updateSleepConfig,
  getSleepEstimate,
  getSleepConfig,
  SleepStatus,
  SleepEstimate,
  SleepConfigData,
} from '../api/client';

// ─── Styles ───────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '900px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '16px', marginBottom: '24px' },
  card: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)' },
  cardTitle: { fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase' as const, letterSpacing: '1px', marginBottom: '8px' },
  cardValue: { fontSize: '28px', fontWeight: 700, color: 'var(--text-primary)' },
  row: { display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' as const },
  btn: {
    padding: '10px 24px', borderRadius: '8px', border: 'none', fontSize: '14px',
    fontWeight: 600, cursor: 'pointer', transition: 'all 0.2s',
  },
  btnPrimary: { background: 'var(--accent)', color: '#fff' },
  btnWarning: { background: 'var(--warning)', color: '#fff' },
  btnDanger: { background: 'var(--error)', color: '#fff' },
  btnOutline: { background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent)' },
  btnDisabled: { background: 'var(--border)', color: 'var(--text-secondary)', cursor: 'not-allowed' },
  section: {
    background: 'var(--bg-card)', borderRadius: '12px', padding: '20px',
    border: '1px solid var(--border)', marginBottom: '16px',
  },
  sectionTitle: { fontSize: '16px', fontWeight: 600, marginBottom: '16px', color: 'var(--text-primary)' },
  label: { fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '4px' },
  input: {
    width: '100%', padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)',
    background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px',
    marginBottom: '12px', boxSizing: 'border-box' as const,
  },
  select: {
    width: '100%', padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)',
    background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '14px',
    marginBottom: '12px',
  },
  progressBar: {
    width: '100%', height: '8px', background: 'var(--border)', borderRadius: '4px',
    overflow: 'hidden', marginTop: '8px',
  },
  progressFill: {
    height: '100%', background: 'var(--accent)', borderRadius: '4px',
    transition: 'width 0.5s ease',
  },
  inputRow: { display: 'flex', gap: '12px', alignItems: 'flex-end', marginBottom: '12px' },
  hint: { fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', fontStyle: 'italic' },
  badge: {
    display: 'inline-block', padding: '4px 12px', borderRadius: '12px',
    fontSize: '12px', fontWeight: 600,
  },
  badgeActive: { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' },
  badgeWarning: { background: 'rgba(255,159,67,0.2)', color: 'var(--warning)' },
  badgeDanger: { background: 'rgba(214,48,49,0.2)', color: 'var(--error)' },
  divider: { height: '1px', background: 'var(--border)', margin: '16px 0' },
  toggle: {
    display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px',
    cursor: 'pointer', userSelect: 'none' as const,
  },
  toggleSwitch: {
    width: '44px', height: '24px', borderRadius: '12px', position: 'relative' as const,
    transition: 'background 0.2s', cursor: 'pointer',
  },
  toggleKnob: {
    width: '20px', height: '20px', borderRadius: '50%', background: '#fff',
    position: 'absolute' as const, top: '2px', transition: 'left 0.2s',
    boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
  },
  estimateGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '12px', marginBottom: '16px' },
  estimateCard: {
    background: 'var(--bg-primary)', borderRadius: '8px', padding: '12px',
    border: '1px solid var(--border)', textAlign: 'center' as const,
  },
  estimateValue: { fontSize: '22px', fontWeight: 700, color: 'var(--accent)' },
  estimateLabel: { fontSize: '11px', color: 'var(--text-secondary)', marginTop: '4px' },
};

// ─── Constants ─────────────────────────────────────────────────

const STAGE_LABELS: Record<string, string> = {
  idle: '—',
  diary_analysis: '📖 Diary Analysis',
  dialog_summarization: '💬 Dialog Summarization',
  user_fact_extraction: '👤 User Fact Extraction',
  verification: '✅ Verification',
  committing: '💾 Committing',
  completed: '✓ Completed',
};

const DURATION_PRESETS = [
  { label: 'Auto (based on data)', value: -1 },
  { label: '5 minutes', value: 5 },
  { label: '15 minutes', value: 15 },
  { label: '30 minutes', value: 30 },
  { label: '1 hour', value: 60 },
  { label: '2 hours', value: 120 },
  { label: 'Indefinite', value: 0 },
];

// ─── Component ─────────────────────────────────────────────────

export default function SleepControl() {
  const [status, setStatus] = useState<SleepStatus | null>(null);
  const [estimate, setEstimate] = useState<SleepEstimate | null>(null);
  const [config, setConfig] = useState<SleepConfigData | null>(null);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [loading, setLoading] = useState(false);

  // Form state
  const [durationMode, setDurationMode] = useState<string>('auto');
  const [customDuration, setCustomDuration] = useState('30');
  const [saveDiary, setSaveDiary] = useState(false);
  const [startHour, setStartHour] = useState('');
  const [endHour, setEndHour] = useState('');
  const [forcedAwake, setForcedAwake] = useState(false);

  // ─── Data Fetching ───────────────────────────────────────

  const fetchStatus = useCallback(async () => {
    try {
      const s = await getSleepStatus();
      setStatus(s);
    } catch {
      // silent — polling
    }
  }, []);

  const fetchEstimate = useCallback(async () => {
    try {
      const e = await getSleepEstimate();
      setEstimate(e);
    } catch {
      // silent
    }
  }, []);

  const fetchConfig = useCallback(async () => {
    try {
      const c = await getSleepConfig();
      setConfig(c);
      setSaveDiary(c.save_processed);
      setStartHour(c.start_hour?.toString() ?? '');
      setEndHour(c.end_hour?.toString() ?? '');
      setForcedAwake(c.forced_awake);
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    fetchEstimate();
    fetchConfig();
    const interval = setInterval(fetchStatus, 3000);
    return () => clearInterval(interval);
  }, [fetchStatus, fetchEstimate, fetchConfig]);

  // ─── Handlers ────────────────────────────────────────────

  const showSuccess = (msg: string) => {
    setSuccessMsg(msg);
    setTimeout(() => setSuccessMsg(''), 4000);
  };

  const handleStartSleep = async (durationMinutes?: number) => {
    try {
      setError('');
      setLoading(true);
      const result = await startSleep(durationMinutes);
      if (!result.success) {
        setError(result.message);
      } else {
        showSuccess('Sleep started successfully');
      }
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start sleep');
    } finally {
      setLoading(false);
    }
  };

  const handleStartWithAutoDuration = async () => {
    if (!estimate || estimate.estimated_minutes <= 0) {
      await handleStartSleep(undefined); // indefinite
    } else {
      // Add 20% buffer to the estimate
      const buffered = Math.ceil(estimate.estimated_minutes * 1.2);
      await handleStartSleep(buffered);
    }
  };

  const handleStartWithCustomDuration = async () => {
    const dur = customDuration ? parseInt(customDuration) : undefined;
    await handleStartSleep(dur || undefined);
  };

  const handleWakeUp = async () => {
    try {
      setError('');
      setLoading(true);
      const result = await wakeUp();
      if (!result.success) setError(result.message);
      else showSuccess('Mila woke up');
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to wake up');
    } finally {
      setLoading(false);
    }
  };

  const handleSaveConfig = async () => {
    try {
      setError('');
      setSuccessMsg('');
      await updateSleepConfig({
        save_processed: saveDiary,
        start_hour: startHour ? parseInt(startHour) : null,
        end_hour: endHour ? parseInt(endHour) : null,
        forced_awake: forcedAwake,
      });
      showSuccess('Configuration saved');
      await fetchConfig();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save config');
    }
  };

  // ─── Derived State ───────────────────────────────────────

  if (!status) return <div style={{ color: 'var(--text-secondary)', padding: '40px', textAlign: 'center' }}>Loading...</div>;

  const isSleeping = status.state === 'sleeping';
  const isAwake = status.state === 'awake';
  const isCommitting = status.state === 'committing';
  const isError = status.state === 'sleep_error';
  const canStartSleep = isAwake && !loading;
  const canWakeUp = (isSleeping || isCommitting) && !loading;

  const stageLabel = STAGE_LABELS[status.current_stage] || status.current_stage;

  const elapsedStr = status.elapsed_seconds
    ? `${Math.floor(status.elapsed_seconds / 60)}m ${Math.floor(status.elapsed_seconds % 60)}s`
    : '—';

  const remainingStr = status.remaining_seconds
    ? `${Math.floor(status.remaining_seconds / 60)}m ${Math.floor(status.remaining_seconds % 60)}s`
    : isSleeping ? 'indefinite' : '—';

  const stateBadge = isSleeping
    ? { style: styles.badgeActive, text: '💤 Sleeping' }
    : isCommitting
    ? { style: styles.badgeWarning, text: '💾 Committing' }
    : isError
    ? { style: styles.badgeDanger, text: '⚠️ Error' }
    : { style: styles.badgeActive, text: '✨ Awake' };

  // ─── Render ──────────────────────────────────────────────

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>💤 Sleep Control</h1>

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

      {/* ── Status Cards ─────────────────────────────────── */}
      <div style={styles.grid}>
        <div style={styles.card}>
          <div style={styles.cardTitle}>State</div>
          <div style={{ ...styles.cardValue, fontSize: '20px' }}>
            <span style={{ ...styles.badge, ...stateBadge.style }}>{stateBadge.text}</span>
          </div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '8px' }}>
            Stage: <strong>{stageLabel}</strong>
          </div>
        </div>

        <div style={styles.card}>
          <div style={styles.cardTitle}>Progress</div>
          <div style={{ ...styles.cardValue, fontSize: '20px' }}>
            {status.progress_pct.toFixed(0)}%
          </div>
          <div style={styles.progressBar}>
            <div style={{ ...styles.progressFill, width: `${status.progress_pct}%` }} />
          </div>
        </div>

        <div style={styles.card}>
          <div style={styles.cardTitle}>Elapsed / Remaining</div>
          <div style={{ ...styles.cardValue, fontSize: '20px' }}>{elapsedStr}</div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Remaining: {remainingStr}
          </div>
        </div>
      </div>

      {/* ── Sleep Controls ────────────────────────────────── */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>🚀 Sleep Controls</h2>

        {isAwake && (
          <>
            {/* Duration mode selector */}
            <div style={{ marginBottom: '16px' }}>
              <div style={styles.label}>Duration mode</div>
              <div style={{ ...styles.row, gap: '8px' }}>
                <button
                  style={{
                    ...styles.btn,
                    ...(durationMode === 'auto' ? styles.btnPrimary : styles.btnOutline),
                    fontSize: '13px', padding: '6px 16px',
                  }}
                  onClick={() => setDurationMode('auto')}
                >
                  🧠 Auto (based on data)
                </button>
                <button
                  style={{
                    ...styles.btn,
                    ...(durationMode === 'custom' ? styles.btnPrimary : styles.btnOutline),
                    fontSize: '13px', padding: '6px 16px',
                  }}
                  onClick={() => setDurationMode('custom')}
                >
                  ⏱️ Custom time
                </button>
              </div>
            </div>

            {/* Auto duration — shows estimate */}
            {durationMode === 'auto' && estimate && (
              <div style={{ marginBottom: '16px' }}>
                <div style={styles.estimateGrid}>
                  <div style={styles.estimateCard}>
                    <div style={styles.estimateValue}>{estimate.diary_entries}</div>
                    <div style={styles.estimateLabel}>Diary entries</div>
                  </div>
                  <div style={styles.estimateCard}>
                    <div style={styles.estimateValue}>{estimate.active_dialogs}</div>
                    <div style={styles.estimateLabel}>Active dialogs</div>
                  </div>
                  <div style={styles.estimateCard}>
                    <div style={styles.estimateValue}>{estimate.total_messages}</div>
                    <div style={styles.estimateLabel}>Total messages</div>
                  </div>
                  <div style={styles.estimateCard}>
                    <div style={styles.estimateValue}>{estimate.estimated_minutes}m</div>
                    <div style={styles.estimateLabel}>Estimated (+20% buffer)</div>
                  </div>
                </div>
                <div style={styles.hint}>
                  Breakdown: diary {estimate.breakdown.diary_analysis_sec}s + 
                  summarization {estimate.breakdown.dialog_summarization_sec}s + 
                  facts {estimate.breakdown.fact_extraction_sec}s + 
                  overhead {estimate.breakdown.overhead_sec}s
                </div>
              </div>
            )}

            {/* Custom duration */}
            {durationMode === 'custom' && (
              <div style={{ marginBottom: '16px' }}>
                <div style={styles.label}>Select duration</div>
                <div style={{ ...styles.row, gap: '8px', marginBottom: '12px' }}>
                  {DURATION_PRESETS.filter(p => p.value > 0).map(preset => (
                    <button
                      key={preset.value}
                      style={{
                        ...styles.btn,
                        ...(customDuration === String(preset.value) ? styles.btnPrimary : styles.btnOutline),
                        fontSize: '13px', padding: '6px 14px',
                      }}
                      onClick={() => setCustomDuration(String(preset.value))}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
                <div style={styles.inputRow}>
                  <input
                    style={{ ...styles.input, width: '160px', marginBottom: 0 }}
                    type="number"
                    min="1"
                    max="1440"
                    value={customDuration}
                    onChange={e => setCustomDuration(e.target.value)}
                    placeholder="Minutes"
                  />
                  <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>minutes</span>
                </div>
              </div>
            )}

            {/* Start button */}
            <div style={styles.row}>
              <button
                style={{ ...styles.btn, ...styles.btnPrimary, ...(!canStartSleep ? styles.btnDisabled : {}) }}
                disabled={!canStartSleep}
                onClick={() => {
                  if (durationMode === 'auto') {
                    handleStartWithAutoDuration();
                  } else {
                    handleStartWithCustomDuration();
                  }
                }}
              >
                {loading ? '⏳ Starting...' : '💤 Start Sleep'}
              </button>
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                {durationMode === 'auto'
                  ? estimate
                    ? `Will sleep ~${Math.ceil(estimate.estimated_minutes * 1.2)} min (auto-estimated)`
                    : 'Will sleep until manually woken'
                  : customDuration
                    ? `Will sleep ${customDuration} min`
                    : 'Will sleep until manually woken'}
              </span>
            </div>
          </>
        )}

        {/* Wake up button — visible when sleeping or committing */}
        {(isSleeping || isCommitting) && (
          <div style={styles.row}>
            <button
              style={{ ...styles.btn, ...styles.btnDanger, ...(!canWakeUp ? styles.btnDisabled : {}) }}
              disabled={!canWakeUp}
              onClick={handleWakeUp}
            >
              {loading ? '⏳ Waking...' : '⏰ Wake Up Now'}
            </button>
            <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              {isCommitting ? 'Commit in progress — will wake after completion' : 'Force wake & rollback changes'}
            </span>
          </div>
        )}

        {/* Error state */}
        {isError && (
          <div style={{ marginTop: '12px' }}>
            <div style={{ fontSize: '13px', color: 'var(--error)', marginBottom: '8px' }}>
              Error: {status.error_message || 'Unknown error'}
            </div>
            <button
              style={{ ...styles.btn, ...styles.btnWarning }}
              onClick={handleWakeUp}
            >
              🔄 Recover & Wake
            </button>
          </div>
        )}
      </div>

      {/* ── Session Info (when sleeping) ──────────────────── */}
      {(isSleeping || isCommitting) && status.session_id && (
        <div style={styles.section}>
          <h2 style={styles.sectionTitle}>📋 Session Info</h2>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '13px' }}>
            <div style={{ color: 'var(--text-secondary)' }}>Session ID:</div>
            <div><code style={{ color: 'var(--accent)', fontSize: '12px' }}>{status.session_id}</code></div>

            <div style={{ color: 'var(--text-secondary)' }}>Triggered by:</div>
            <div style={{ color: 'var(--text-primary)' }}><strong>{status.triggered_by}</strong></div>

            <div style={{ color: 'var(--text-secondary)' }}>Messages queued:</div>
            <div style={{ color: 'var(--text-primary)' }}><strong>{status.queue_count}</strong></div>

            {status.started_at && (
              <>
                <div style={{ color: 'var(--text-secondary)' }}>Started:</div>
                <div style={{ color: 'var(--text-primary)' }}>{new Date(status.started_at).toLocaleString()}</div>
              </>
            )}

            {status.scheduled_end_at && (
              <>
                <div style={{ color: 'var(--text-secondary)' }}>Scheduled end:</div>
                <div style={{ color: 'var(--text-primary)' }}>{new Date(status.scheduled_end_at).toLocaleString()}</div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Configuration ─────────────────────────────────── */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>⚙️ Sleep Configuration</h2>

        {/* Forced awake toggle */}
        <div style={styles.toggle} onClick={() => setForcedAwake(!forcedAwake)}>
          <div style={{
            ...styles.toggleSwitch,
            background: forcedAwake ? 'var(--error)' : 'var(--border)',
          }}>
            <div style={{ ...styles.toggleKnob, left: forcedAwake ? '22px' : '2px' }} />
          </div>
          <div>
            <div style={{ fontSize: '14px', color: 'var(--text-primary)', fontWeight: 500 }}>
              {forcedAwake ? '🔴 Forced Awake (sleep disabled)' : '🟢 Sleep Allowed'}
            </div>
            <div style={styles.hint}>
              {forcedAwake
                ? 'Mila will NOT sleep automatically or manually until this is turned off'
                : 'Mila can sleep on schedule or manually'}
            </div>
          </div>
        </div>

        <div style={styles.divider} />

        {/* Auto-sleep schedule */}
        <div style={{ marginBottom: '16px' }}>
          <div style={{ ...styles.label, fontWeight: 600 }}>Auto-Sleep Schedule</div>
          <div style={styles.hint}>(Leave empty to disable auto-sleep)</div>
        </div>

        <div style={styles.inputRow}>
          <div style={{ flex: 1 }}>
            <div style={styles.label}>Start hour (0–23)</div>
            <input
              style={styles.input}
              type="number"
              min="0"
              max="23"
              value={startHour}
              onChange={e => setStartHour(e.target.value)}
              placeholder="e.g., 22"
            />
          </div>
          <div style={{ flex: 1 }}>
            <div style={styles.label}>End hour (0–23)</div>
            <input
              style={styles.input}
              type="number"
              min="0"
              max="23"
              value={endHour}
              onChange={e => setEndHour(e.target.value)}
              placeholder="e.g., 7"
            />
          </div>
        </div>

        <div style={styles.divider} />

        {/* Diary saving */}
        <div style={{ marginBottom: '16px' }}>
          <div style={styles.label}>Save processed diaries to logs</div>
          <select
            style={styles.select}
            value={saveDiary ? 'true' : 'false'}
            onChange={e => setSaveDiary(e.target.value === 'true')}
          >
            <option value="false">Off — diary cleared after sleep</option>
            <option value="true">On — save copy before clearing</option>
          </select>
        </div>

        <button
          style={{ ...styles.btn, ...styles.btnPrimary }}
          onClick={handleSaveConfig}
        >
          💾 Save Configuration
        </button>
      </div>
    </div>
  );
}