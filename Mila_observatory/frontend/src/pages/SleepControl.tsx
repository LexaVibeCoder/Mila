import React, { useEffect, useState, useCallback } from 'react';
import {
  getSleepStatus,
  startSleep,
  wakeUp,
  updateSleepConfig,
  SleepStatus,
} from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '900px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px', marginBottom: '24px' },
  card: { background: 'var(--bg-card)', borderRadius: '12px', padding: '20px', border: '1px solid var(--border)' },
  cardTitle: { fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '8px' },
  cardValue: { fontSize: '28px', fontWeight: 700, color: 'var(--text-primary)' },
  row: { display: 'flex', gap: '12px', alignItems: 'center', flexWrap: 'wrap' as const },
  btn: {
    padding: '10px 24px', borderRadius: '8px', border: 'none', fontSize: '14px',
    fontWeight: 600, cursor: 'pointer', transition: 'all 0.2s',
  },
  btnSleep: { background: 'var(--accent)', color: '#fff' },
  btnWake: { background: 'var(--warning)', color: '#fff' },
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
    marginBottom: '12px',
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
  stageBadge: {
    display: 'inline-block', padding: '4px 12px', borderRadius: '12px',
    fontSize: '12px', fontWeight: 600, background: 'rgba(0,184,148,0.2)', color: 'var(--success)',
  },
  inputRow: { display: 'flex', gap: '12px', alignItems: 'flex-end', marginBottom: '12px' },
};

const STAGE_LABELS: Record<string, string> = {
  idle: '—',
  diary_analysis: '📖 Diary Analysis',
  dialog_summarization: '💬 Dialog Summarization',
  user_fact_extraction: '👤 User Fact Extraction',
  verification: '✅ Verification',
  committing: '💾 Committing',
  completed: '✓ Completed',
};

export default function SleepControl() {
  const [status, setStatus] = useState<SleepStatus | null>(null);
  const [error, setError] = useState('');
  const [duration, setDuration] = useState('60');
  const [saveDiary, setSaveDiary] = useState(false);
  const [startHour, setStartHour] = useState('');
  const [endHour, setEndHour] = useState('');

  const fetchStatus = useCallback(async () => {
    try {
      const s = await getSleepStatus();
      setStatus(s);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 3000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const handleStartSleep = async () => {
    try {
      setError('');
      const dur = duration ? parseInt(duration) : undefined;
      const result = await startSleep(dur);
      if (!result.success) setError(result.message);
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start sleep');
    }
  };

  const handleWakeUp = async () => {
    try {
      setError('');
      const result = await wakeUp();
      if (!result.success) setError(result.message);
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to wake up');
    }
  };

  const handleSaveConfig = async () => {
    try {
      setError('');
      await updateSleepConfig({
        save_processed: saveDiary,
        start_hour: startHour ? parseInt(startHour) : null,
        end_hour: endHour ? parseInt(endHour) : null,
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save config');
    }
  };

  if (!status) return <div style={{ color: 'var(--text-secondary)' }}>Loading...</div>;

  const isSleeping = status.state === 'sleeping';
  const isAwake = status.state === 'awake';
  const isCommitting = status.state === 'committing';
  const canStartSleep = isAwake;
  const canWakeUp = isSleeping;

  const stageLabel = STAGE_LABELS[status.current_stage] || status.current_stage;

  const elapsedStr = status.elapsed_seconds
    ? `${Math.floor(status.elapsed_seconds / 60)}m ${Math.floor(status.elapsed_seconds % 60)}s`
    : '—';

  const remainingStr = status.remaining_seconds
    ? `${Math.floor(status.remaining_seconds / 60)}m ${Math.floor(status.remaining_seconds % 60)}s`
    : '—';

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>💤 Sleep Control</h1>

      {error && (
        <div style={{ color: 'var(--error)', marginBottom: '16px', fontSize: '13px' }}>
          {error}
        </div>
      )}

      {/* Status Card */}
      <div style={styles.grid}>
        <div style={styles.card}>
          <div style={styles.cardTitle}>State</div>
          <div style={{ ...styles.cardValue, fontSize: '20px' }}>
            {isSleeping ? '💤 Sleeping' : isCommitting ? '💾 Committing' : '✨ Awake'}
          </div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Stage: {stageLabel}
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
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            Remaining: {remainingStr}
          </div>
        </div>
      </div>

      {/* Controls */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Controls</h2>
        <div style={styles.row}>
          <button
            style={{ ...styles.btn, ...(canStartSleep ? styles.btnSleep : styles.btnDisabled) }}
            disabled={!canStartSleep}
            onClick={handleStartSleep}
          >
            💤 Start Sleep
          </button>
          <button
            style={{ ...styles.btn, ...(canWakeUp ? styles.btnWake : styles.btnDisabled) }}
            disabled={!canWakeUp}
            onClick={handleWakeUp}
          >
            ⏰ Wake Up
          </button>
        </div>

        {isAwake && (
          <div style={{ marginTop: '16px' }}>
            <div style={styles.label}>Duration (minutes, leave empty for indefinite)</div>
            <div style={styles.inputRow}>
              <input
                style={{ ...styles.input, width: '200px', marginBottom: 0 }}
                type="number"
                min="1"
                max="1440"
                value={duration}
                onChange={e => setDuration(e.target.value)}
                placeholder="Duration in minutes"
              />
              <button
                style={{ ...styles.btn, ...styles.btnSleep }}
                onClick={() => {
                  const dur = duration ? parseInt(duration) : undefined;
                  startSleep(dur).then(fetchStatus);
                }}
              >
                Start with Duration
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Current Session Info */}
      {isSleeping && status.session_id && (
        <div style={styles.section}>
          <h2 style={styles.sectionTitle}>Session Info</h2>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
            Session ID: <code style={{ color: 'var(--accent)' }}>{status.session_id}</code>
          </div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
            Triggered by: <strong>{status.triggered_by}</strong>
          </div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
            Messages in queue: <strong>{status.queue_count}</strong>
          </div>
          {status.started_at && (
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              Started: {new Date(status.started_at).toLocaleString()}
            </div>
          )}
        </div>
      )}

      {/* Configuration */}
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>Configuration</h2>

        <div style={{ marginBottom: '16px' }}>
          <div style={styles.label}>Save processed diaries to logs</div>
          <select
            style={styles.select}
            value={saveDiary ? 'true' : 'false'}
            onChange={e => setSaveDiary(e.target.value === 'true')}
          >
            <option value="false">Off</option>
            <option value="true">On</option>
          </select>
        </div>

        <div style={styles.inputRow}>
          <div style={{ flex: 1 }}>
            <div style={styles.label}>Auto-sleep start hour (0-23)</div>
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
            <div style={styles.label}>Auto-sleep end hour (0-23)</div>
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

        <button
          style={{ ...styles.btn, background: 'var(--accent)', color: '#fff' }}
          onClick={handleSaveConfig}
        >
          Save Configuration
        </button>
      </div>
    </div>
  );
}