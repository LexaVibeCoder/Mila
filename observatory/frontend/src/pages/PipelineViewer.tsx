import React, { useEffect, useState } from 'react';
import { getPipelineLogs, getPipelineLog, PipelineLogItem } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1200px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  toolbar: { display: 'flex', gap: '12px', marginBottom: '16px' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px' },
  td: { padding: '10px 12px', borderBottom: '1px solid var(--border)', fontSize: '13px', verticalAlign: 'top' },
  badge: { display: 'inline-block', padding: '2px 6px', borderRadius: '4px', fontSize: '11px' },
  badgeSuccess: { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' },
  badgeError: { background: 'rgba(225,112,85,0.2)', color: 'var(--error)' },
  modal: { position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 },
  modalContent: { background: 'var(--bg-card)', borderRadius: '12px', padding: '24px', width: '800px', maxWidth: '90vw', maxHeight: '80vh', overflow: 'auto' },
  pre: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '12px', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', overflow: 'auto', maxHeight: '500px', color: 'var(--text-primary)' },
  pagination: { display: 'flex', gap: '8px', alignItems: 'center', marginTop: '16px' },
};

export default function PipelineViewer() {
  const [logs, setLogs] = useState<PipelineLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [selectedLog, setSelectedLog] = useState<{ filename: string; data: string } | null>(null);
  const limit = 20;

  useEffect(() => {
    getPipelineLogs(page, limit).then(r => {
      setLogs(r.items);
      setTotal(r.total);
    });
  }, [page]);

  const handleView = async (filename: string) => {
    try {
      const r = await getPipelineLog(filename);
      setSelectedLog({ filename: r.filename, data: JSON.stringify(r.data, null, 2) });
    } catch (e) { console.error(e); }
  };

  const totalPages = Math.ceil(total / limit);

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Pipeline Logs</h1>
      <div style={styles.toolbar}>
        <button style={styles.btnSecondary} onClick={() => getPipelineLogs(page, limit).then(r => { setLogs(r.items); setTotal(r.total); })}>Refresh</button>
      </div>

      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Time</th>
            <th style={styles.th}>Message</th>
            <th style={styles.th}>Status</th>
            <th style={styles.th}>Actions</th>
            <th style={styles.th}>Time</th>
          </tr>
        </thead>
        <tbody>
          {logs.map(log => (
            <tr key={log.filename}>
              <td style={styles.td}>{log.timestamp?.slice(11, 19) || '-'}</td>
              <td style={styles.td}>{log.user_message?.slice(0, 80) || '-'}</td>
              <td style={styles.td}>
                <span style={{ ...styles.badge, ...(log.success ? styles.badgeSuccess : styles.badgeError) }}>
                  {log.success ? 'OK' : 'ERR'}
                </span>
              </td>
              <td style={styles.td}>{log.actions_count}</td>
              <td style={styles.td}>
                <button style={{ ...styles.btn, padding: '4px 10px', fontSize: '12px' }} onClick={() => handleView(log.filename)}>View</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {totalPages > 1 && (
        <div style={styles.pagination}>
          <button style={styles.btnSecondary} disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
          <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Page {page} of {totalPages}</span>
          <button style={styles.btnSecondary} disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}

      {selectedLog && (
        <div style={styles.modal} onClick={() => setSelectedLog(null)}>
          <div style={styles.modalContent} onClick={e => e.stopPropagation()}>
            <h2 style={{ marginBottom: '12px', fontSize: '14px', color: 'var(--text-primary)' }}>{selectedLog.filename}</h2>
            <pre style={styles.pre}>{selectedLog.data}</pre>
            <button style={{ ...styles.btn, marginTop: '12px' }} onClick={() => setSelectedLog(null)}>Close</button>
          </div>
        </div>
      )}
    </div>
  );
}