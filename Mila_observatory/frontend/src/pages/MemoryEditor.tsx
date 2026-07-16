import React, { useEffect, useState, useCallback } from 'react';
import { getMemories, deleteMemory, updateMemory, createMemory, rebuildFaiss, MemoryItem } from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1200px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '24px', color: 'var(--text-primary)' },
  toolbar: { display: 'flex', gap: '12px', marginBottom: '16px', flexWrap: 'wrap' },
  input: { padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px', flex: 1, minWidth: '200px' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600, whiteSpace: 'nowrap' },
  btnDanger: { padding: '8px 16px', background: 'var(--error)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px' },
  td: { padding: '10px 12px', borderBottom: '1px solid var(--border)', fontSize: '13px', verticalAlign: 'top' },
  contentCell: { maxWidth: '400px', overflow: 'hidden', textOverflow: 'ellipsis' },
  badge: { display: 'inline-block', padding: '2px 6px', borderRadius: '4px', fontSize: '11px', background: 'rgba(108,92,231,0.2)', color: 'var(--accent)' },
  modal: { position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 },
  modalContent: { background: 'var(--bg-card)', borderRadius: '12px', padding: '24px', width: '600px', maxWidth: '90vw', maxHeight: '80vh', overflow: 'auto' },
  textarea: { width: '100%', padding: '8px 12px', background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '6px', color: 'var(--text-primary)', fontSize: '14px', minHeight: '100px', fontFamily: 'monospace' },
  field: { marginBottom: '12px' },
  label: { display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' },
  pagination: { display: 'flex', gap: '8px', alignItems: 'center', marginTop: '16px' },
};

export default function MemoryEditor() {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [editItem, setEditItem] = useState<MemoryItem | null>(null);
  const [editContent, setEditContent] = useState('');
  const [newContent, setNewContent] = useState('');
  const [msg, setMsg] = useState('');
  const limit = 20;

  const load = useCallback(async () => {
    try {
      const r = await getMemories(query || undefined, page, limit);
      setItems(r.items);
      setTotal(r.total);
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  }, [query, page]);

  useEffect(() => { load(); }, [load]);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this memory?')) return;
    try {
      await deleteMemory(id);
      setMsg('Memory deleted');
      load();
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const handleEdit = (item: MemoryItem) => {
    setEditItem(item);
    setEditContent(item.content);
  };

  const handleSaveEdit = async () => {
    if (!editItem) return;
    try {
      await updateMemory(editItem.mem_id, { content: editContent });
      setEditItem(null);
      setMsg('Memory updated');
      load();
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const handleCreate = async () => {
    if (!newContent.trim()) return;
    try {
      await createMemory({ content: newContent });
      setNewContent('');
      setMsg('Memory created');
      load();
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const handleRebuild = async () => {
    if (!confirm('Rebuild FAISS index?')) return;
    try {
      const r = await rebuildFaiss();
      setMsg(`FAISS rebuilt: ${r.count} vectors`);
    } catch (e: unknown) { setMsg(`Error: ${e instanceof Error ? e.message : e}`); }
  };

  const totalPages = Math.ceil(total / limit);

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Memory Editor</h1>
      {msg && <div style={{ color: 'var(--success)', marginBottom: '12px', fontSize: '13px' }}>{msg}</div>}

      <div style={styles.toolbar}>
        <input style={styles.input} placeholder="Search..." value={query} onChange={e => { setQuery(e.target.value); setPage(1); }} />
        <button style={styles.btnSecondary} onClick={handleRebuild}>Rebuild FAISS</button>
      </div>

      <div style={{ ...styles.toolbar }}>
        <input style={{ ...styles.input, flex: 1 }} placeholder="New memory..." value={newContent} onChange={e => setNewContent(e.target.value)} />
        <button style={styles.btn} onClick={handleCreate}>Add</button>
      </div>

      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Content</th>
            <th style={styles.th}>Type</th>
            <th style={styles.th}>Confidence</th>
            <th style={styles.th}>Created</th>
            <th style={styles.th}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map(item => (
            <tr key={item.mem_id}>
              <td style={styles.td}><div style={styles.contentCell}>{item.content}</div></td>
              <td style={styles.td}><span style={styles.badge}>{item.type}</span></td>
              <td style={styles.td}>{(item.confidence * 100).toFixed(0)}%</td>
              <td style={styles.td}>{item.created_at?.slice(0, 10) || '-'}</td>
              <td style={styles.td}>
                <button style={{ ...styles.btn, marginRight: '6px', padding: '4px 10px', fontSize: '12px' }} onClick={() => handleEdit(item)}>Edit</button>
                <button style={{ ...styles.btnDanger, padding: '4px 10px', fontSize: '12px' }} onClick={() => handleDelete(item.mem_id)}>Del</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {totalPages > 1 && (
        <div style={styles.pagination}>
          <button style={styles.btnSecondary} disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
          <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Page {page} of {totalPages} ({total} total)</span>
          <button style={styles.btnSecondary} disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}

      {editItem && (
        <div style={styles.modal} onClick={() => setEditItem(null)}>
          <div style={styles.modalContent} onClick={e => e.stopPropagation()}>
            <h2 style={{ marginBottom: '16px', fontSize: '16px', color: 'var(--text-primary)' }}>Edit Memory</h2>
            <div style={styles.field}>
              <label style={styles.label}>Content</label>
              <textarea style={styles.textarea} value={editContent} onChange={e => setEditContent(e.target.value)} />
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button style={styles.btn} onClick={handleSaveEdit}>Save</button>
              <button style={styles.btnSecondary} onClick={() => setEditItem(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}