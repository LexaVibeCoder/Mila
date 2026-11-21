import React, { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  getPipelineLogs,
  getPipelineLog,
  getPipelineChats,
  PipelineLogItem,
  PipelineChatItem,
  PipelineLogDetail,
  PipelineMessage,
  PipelineRawResponse,
} from '../api/client';

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: '1200px' },
  title: { fontSize: '24px', fontWeight: 700, marginBottom: '16px', color: 'var(--text-primary)' },
  layout: { display: 'flex', gap: '16px', alignItems: 'flex-start' },
  chatList: { width: '240px', flexShrink: 0, background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: '12px', padding: '12px', maxHeight: '70vh', overflow: 'auto' },
  chatListTitle: { fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: '8px' },
  chatItem: { display: 'block', width: '100%', textAlign: 'left', padding: '8px 10px', borderRadius: '8px', border: '1px solid transparent', background: 'transparent', color: 'var(--text-primary)', cursor: 'pointer', fontSize: '13px', marginBottom: '4px' },
  chatItemActive: { background: 'var(--bg-hover)', borderColor: 'var(--border)', color: 'var(--accent)' },
  chatMeta: { fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const },
  main: { flex: 1, minWidth: 0 },
  toolbar: { display: 'flex', gap: '12px', marginBottom: '16px', alignItems: 'center' },
  btn: { padding: '8px 16px', background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600 },
  btnSecondary: { padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px' },
  td: { padding: '10px 12px', borderBottom: '1px solid var(--border)', fontSize: '13px', verticalAlign: 'top' },
  badge: { display: 'inline-block', padding: '2px 6px', borderRadius: '4px', fontSize: '11px' },
  badgeSuccess: { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' },
  badgeError: { background: 'rgba(225,112,85,0.2)', color: 'var(--error)' },
  modal: { position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 },
  modalContent: { background: 'var(--bg-card)', borderRadius: '12px', padding: '24px', width: '900px', maxWidth: '92vw', maxHeight: '85vh', overflow: 'auto' },
  sectionTitle: { fontSize: '13px', fontWeight: 700, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '1px', margin: '20px 0 8px' },
  pre: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '12px', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflow: 'auto', maxHeight: '300px', color: 'var(--text-primary)', margin: 0 },
  meta: { fontSize: '12px', color: 'var(--text-secondary)' },
  details: { margin: '8px 0', fontSize: '13px' },
  msgCard: { background: 'var(--bg-secondary)', borderRadius: '8px', padding: '10px 12px', marginBottom: '8px', border: '1px solid var(--border)' },
  msgBody: { fontSize: '13px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text-primary)', marginTop: '6px' },
  roleBadge: { display: 'inline-block', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px' },
  pagination: { display: 'flex', gap: '8px', alignItems: 'center', marginTop: '16px' },
};

function roleStyle(role: string): React.CSSProperties {
  const r = (role || '').toLowerCase();
  if (r === 'user') return { background: 'rgba(116,185,255,0.2)', color: '#74b9ff' };
  if (r === 'assistant') return { background: 'rgba(0,184,148,0.2)', color: 'var(--success)' };
  if (r === 'system') return { background: 'rgba(178,190,195,0.2)', color: 'var(--text-secondary)' };
  if (r === 'tool') return { background: 'rgba(253,203,110,0.2)', color: '#fdcb6e' };
  return { background: 'var(--bg-hover)', color: 'var(--text-primary)' };
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  // "2024-01-01T12:34:56.789" -> "01-01 12:34:56"
  const d = iso.slice(0, 10);
  const t = iso.slice(11, 19);
  if (d && t) return `${d.slice(5)} ${t}`;
  return iso;
}

function shortChat(chatId: string): string {
  if (!chatId) return '—';
  if (chatId === '(unknown)') return '(unknown)';
  return chatId.length > 18 ? `${chatId.slice(0, 8)}…${chatId.slice(-6)}` : chatId;
}

function chatLabel(displayName: string | undefined, chatId: string): string {
  if (displayName) return displayName;
  return shortChat(chatId);
}

// Reasoning is normally a string; some gateways return structured details.
function renderReasoning(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// Multimodal messages arrive as OpenAI content blocks
// ([{type:'text',text},{type:'image_url',...},{type:'input_audio',...}]).
// No preview needed — short labels only. Never return an object
// (React error #31: objects are not valid as a child).
function renderMessageContent(content: unknown): string {
  if (content === null || content === undefined) return '—';
  if (typeof content === 'string') return content || '—';
  const parts = Array.isArray(content) ? content : [content];
  const out: string[] = [];
  for (const p of parts) {
    if (typeof p === 'string') { out.push(p); continue; }
    if (typeof p !== 'object' || p === null) continue;
    const t = (p as Record<string, unknown>).type;
    if (t === 'text') out.push(String((p as Record<string, unknown>).text ?? ''));
    else if (t === 'image_url') out.push('[фото]');
    else if (t === 'input_audio') out.push('[голосовое сообщение]');
    else if (typeof t === 'string' && t) out.push(`[${t}]`);
  }
  const s = out.filter(Boolean).join('\n');
  return s || '—';
}

function MessageCards({ messages, offset = 0 }: { messages: PipelineMessage[]; offset?: number }) {
  return <>{messages.map((message, i) => (
    <div key={i} style={styles.msgCard}>
      <span style={{ ...styles.roleBadge, ...roleStyle(message.role) }}>{message.role}</span>
      <span style={{ ...styles.meta, marginLeft: '8px' }}>#{offset + i + 1}</span>
      <div style={styles.msgBody}>{renderMessageContent(message.content)}</div>
    </div>
  ))}</>;
}

function RawResponse({ response }: { response?: PipelineRawResponse }) {
  if (!response) return <p style={styles.meta}>Ответ LLM отсутствует.</p>;
  const reasoning = renderReasoning(response.reasoning);
  return (
    <>
      <p style={styles.meta}>Ответ LLM · {response.finish_reason || 'причина завершения не указана'}</p>
      <pre style={styles.pre}>{response.thoughts || 'Пустой текст ответа'}</pre>
      {reasoning && (
        <details style={styles.details}>
          <summary>Reasoning</summary>
          <pre style={styles.pre}>{reasoning}</pre>
        </details>
      )}
      {(response.tool_calls || []).map((call, i) => (
        <div key={i} style={styles.details}>
          <div style={{ ...styles.roleBadge, ...roleStyle('tool') }}>
            {call.name}{call.id && ` · ${call.id}`}
          </div>
          <pre style={styles.pre}>{call.arguments || '—'}</pre>
        </div>
      ))}
    </>
  );
}

function ActionResults({ actions }: { actions: PipelineLogDetail['actions'] }) {
  return <>{actions.map(({ decision_review, ...action }, i) => (
    <div key={i}>
      <details style={styles.details}>
        <summary>{String(action.type ?? action.action_type ?? 'Действие')} · {String(action.status ?? '—')}</summary>
        <pre style={styles.pre}>{JSON.stringify(action, null, 2)}</pre>
      </details>
      {!!decision_review && (
        <details style={{ ...styles.details, marginLeft: '16px' }}>
          <summary>Проверка дневника decision-моделью</summary>
          <pre style={styles.pre}>{JSON.stringify(decision_review, null, 2)}</pre>
        </details>
      )}
    </div>
  ))}</>;
}

export default function PipelineViewer() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [logs, setLogs] = useState<PipelineLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [chats, setChats] = useState<PipelineChatItem[]>([]);
  const [activeChat, setActiveChat] = useState<string>('');
  const [selected, setSelected] = useState<{ filename: string; data: PipelineLogDetail } | null>(null);
  const [loading, setLoading] = useState(false);
  const limit = 20;

  const loadChats = useCallback(async () => {
    try {
      const r = await getPipelineChats();
      setChats(r.items);
    } catch (e) { console.error(e); }
  }, []);

  const loadLogs = useCallback(async (p: number, chatId: string) => {
    setLoading(true);
    try {
      const r = await getPipelineLogs(p, limit, undefined, chatId || undefined);
      setLogs(r.items);
      setTotal(r.total);
    } catch (e) { console.error(e); } finally { setLoading(false); }
  }, []);

  useEffect(() => { loadChats(); }, [loadChats]);
  useEffect(() => { loadLogs(page, activeChat); }, [page, activeChat, loadLogs]);

  const handleSelectChat = (chatId: string) => {
    setActiveChat(chatId);
    setPage(1);
  };

  const handleView = async (filename: string) => {
    try {
      const r = await getPipelineLog(filename);
      setSelected({ filename: r.filename, data: r.data });
      setSearchParams({ log: filename }, { replace: true });
    } catch (e) { console.error(e); }
  };

  // Deep-link support: /pipeline?log=<filename> (e.g. from Users page)
  useEffect(() => {
    const f = searchParams.get('log');
    if (f) handleView(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const totalPages = Math.ceil(total / limit);
  const activeChatInfo = chats.find(c => c.chat_id === activeChat);

  const renderDetail = () => {
    if (!selected) return null;
    const d = selected.data || ({} as PipelineLogDetail);
    const messages = Array.isArray(d.llm_messages) ? d.llm_messages : [];
    const actions = Array.isArray(d.actions) ? d.actions : [];
    const rawResponses = Array.isArray(d.raw_llm_responses) ? d.raw_llm_responses : [];
    const requests = d.llm_iterations || [];
    const initialRequest = requests[0];
    const legacyLog = !d.turn_id && !initialRequest && rawResponses.length > 0;
    const hasModelTurn = !d.proactive || !!initialRequest || rawResponses.length > 0;
    const recentStart = Math.max(0, messages.length - 5);
    const triggerLabels = { message: 'Сообщение', notification: 'Уведомление', proactive: 'Проактивность' };
    let actionOffset = 0;
    let contextSize = messages.length;
    const detailName =
      logs.find(l => l.filename === selected.filename)?.display_name ||
      chats.find(c => c.chat_id === d.chat_id)?.display_name ||
      '';
    return (
      <div style={styles.modal} onClick={() => setSelected(null)}>
        <div style={styles.modalContent} onClick={e => e.stopPropagation()}>
          <h2 style={{ margin: 0, fontSize: '15px', color: 'var(--text-primary)' }}>{selected.filename}</h2>
          <div style={{ ...styles.meta, marginTop: '4px' }}>
            {d.timestamp || ''} · {chatLabel(detailName, d.chat_id)} · user <code>{d.user_id || '—'}</code>
            {' · '}<span style={{ ...styles.badge, ...(d.success ? styles.badgeSuccess : styles.badgeError) }}>{d.success ? 'OK' : 'ERR'}</span>
          </div>
          <p style={styles.meta}>
            {d.turn_id && <>Ход <code>{d.turn_id}</code> · </>}
            {String(d.timing?.total ?? '—')} · Итерации {String(d.timing?.iterations ?? rawResponses.length)}
            {d.timing?.max_iterations != null && ` / ${d.timing.max_iterations}`}
          </p>
          {initialRequest && (
            <p style={styles.meta}>
              {initialRequest.model || '—'} · {initialRequest.state} · контекст чата <code>{initialRequest.active_chat_id}</code>
            </p>
          )}
          {d.error && (
            <pre style={{ ...styles.pre, color: 'var(--error)' }}>
              {d.error_code && `${d.error_code} · `}{d.error}
            </pre>
          )}
          {legacyLog && <p style={styles.meta}>Этот лог записан в старом формате. Изменения контекста по итерациям в нём отсутствуют.</p>}

          {d.proactive && (
            <>
              <div style={styles.sectionTitle}>Проактивное решение</div>
              <p style={styles.meta}>
                {d.proactive.action}
                {d.proactive.forced && ' · запуск из Observatory'}
                {d.proactive.dry_run && ' · симуляция'}
                {d.proactive.idle_seconds != null && ` · тишина ${d.proactive.idle_seconds} с`}
                {d.timing?.proactive_total_ms != null && ` · ${d.timing.proactive_total_ms} мс`}
              </p>
              {(d.proactive.outcome || d.proactive.summary) && (
                <p style={styles.meta}>{d.proactive.outcome || d.proactive.summary}</p>
              )}
              {d.proactive.delivery && (
                <p style={styles.meta}>
                  Доставка в чат <code>{d.proactive.delivery.target}</code> · отправлено {d.proactive.delivery.sent}
                  {d.proactive.delivery.error && ` · ${d.proactive.delivery.error}`}
                </p>
              )}
              <details style={styles.details}>
                <summary>Сведения о проактивном ходе</summary>
                <pre style={styles.pre}>{JSON.stringify(d.proactive, null, 2)}</pre>
              </details>
            </>
          )}

          {hasModelTurn && (
            <>
              <div style={styles.sectionTitle}>
                Системный промпт{initialRequest ? ' · итерация 1' : legacyLog ? ' · последняя итерация' : ''}
              </div>
              <details style={styles.details}>
                <summary>Полный промпт ({(d.system_prompt || '').length} симв.)</summary>
                <pre style={styles.pre}>{d.system_prompt || '—'}</pre>
              </details>

              <div style={styles.sectionTitle}>Последние сообщения в контексте</div>
              {recentStart > 0 && (
                <details style={styles.details}>
                  <summary>Более ранний контекст ({recentStart})</summary>
                  <MessageCards messages={messages.slice(0, recentStart)} />
                </details>
              )}
              {messages.length ? <MessageCards messages={messages.slice(recentStart)} offset={recentStart} />
                : <p style={styles.meta}>В логе нет сообщений контекста.</p>}

              <div style={styles.sectionTitle}>
                Инициатор запроса{d.trigger && ` · ${triggerLabels[d.trigger.kind]}`}
              </div>
              <pre style={styles.pre}>{d.trigger?.text || d.user_message || '—'}</pre>

              <div style={styles.sectionTitle}>Итерации LLM</div>
              {requests.map(request => {
                const actionsForRequest = actions.slice(actionOffset, actionOffset + request.action_count);
                actionOffset += request.action_count;
                const delta = request.context_delta;
                const addedAt = contextSize - (delta?.removed_count ?? 0);
                if (delta) contextSize = addedAt + delta.added_messages.length;
                return (
                  <div key={request.iteration} style={styles.msgCard}>
                    <strong>Итерация {request.iteration}</strong>
                    <div style={styles.meta}>
                      {request.iteration > 1 && <>
                        {request.model || '—'} · {request.state} · чат <code>{request.active_chat_id}</code>{' · '}
                      </>}
                      {request.duration_ms != null && `${request.duration_ms} мс`}
                    </div>
                    {delta && (
                      <>
                        <p style={styles.meta}>
                          Контекст к этой итерации · добавлено {delta.added_messages.length} · удалено {delta.removed_count}
                        </p>
                        <MessageCards messages={delta.added_messages} offset={addedAt} />
                      </>
                    )}
                    {request.system_prompt_diff && (
                      <details style={styles.details}>
                        <summary>Изменения системного промпта</summary>
                        <pre style={styles.pre}>{request.system_prompt_diff}</pre>
                      </details>
                    )}
                    <details style={styles.details}>
                      <summary>Параметры запроса и сведения провайдера</summary>
                      <pre style={styles.pre}>{JSON.stringify({
                        max_tokens: request.max_tokens,
                        temperature: request.temperature,
                        reasoning_effort: request.reasoning_effort,
                        tool_choice: request.tool_choice,
                        tools: request.tools,
                        provider: request.provider,
                      }, null, 2)}</pre>
                    </details>
                    <RawResponse response={request.response} />
                    <ActionResults actions={actionsForRequest} />
                  </div>
                );
              })}
              {!requests.length && rawResponses.map((response, i) => (
                <div key={i} style={styles.msgCard}>
                  <strong>Итерация {response.iteration ?? i + 1}</strong>
                  <RawResponse response={response} />
                </div>
              ))}
              {!requests.length && <ActionResults actions={actions} />}
            </>
          )}

          {(d.reply_text || d.thoughts || d.silence_reason) && (
            <>
              <div style={styles.sectionTitle}>Решение Pipeline</div>
              {d.reply_via && <p style={styles.meta}>{d.reply_via} · чат <code>{d.reply_chat_id || '—'}</code></p>}
              <pre style={styles.pre}>{d.silence_reason || d.reply_text || d.thoughts}</pre>
            </>
          )}

          {d.traceback && (
            <details style={styles.details}>
              <summary>Traceback</summary>
              <pre style={{ ...styles.pre, color: 'var(--error)' }}>{d.traceback}</pre>
            </details>
          )}

          <button style={{ ...styles.btn, marginTop: '16px' }} onClick={() => setSelected(null)}>Close</button>
        </div>
      </div>
    );
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Pipeline Logs</h1>
      <div style={styles.layout}>
        <aside style={styles.chatList}>
          <div style={styles.chatListTitle}>Chats · {chats.length}</div>
          <button
            style={{ ...styles.chatItem, ...(activeChat === '' ? styles.chatItemActive : {}) }}
            onClick={() => handleSelectChat('')}
          >
            All chats
            <div style={styles.chatMeta}>{total} logs</div>
          </button>
          {chats.map(c => (
            <button
              key={c.chat_id}
              title={c.chat_id}
              style={{ ...styles.chatItem, ...(activeChat === c.chat_id ? styles.chatItemActive : {}) }}
              onClick={() => handleSelectChat(c.chat_id)}
            >
              {chatLabel(c.display_name, c.chat_id)}
              <div style={styles.chatMeta}>{c.count} msgs · {c.last_message?.slice(0, 40) || ''}</div>
            </button>
          ))}
        </aside>

        <div style={styles.main}>
          <div style={styles.toolbar}>
            <button style={styles.btnSecondary} onClick={() => { loadLogs(page, activeChat); loadChats(); }}>Refresh</button>
            {activeChat && (
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }} title={activeChat}>
                {chatLabel(activeChatInfo?.display_name, activeChat)}
                {activeChatInfo?.user_id && <> · user <code>{activeChatInfo.user_id}</code></>}
              </span>
            )}
            {loading && <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Loading…</span>}
          </div>

          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Time</th>
                <th style={styles.th}>Chat</th>
                <th style={styles.th}>Message</th>
                <th style={styles.th}>Status</th>
                <th style={styles.th}>Actions</th>
                <th style={styles.th}>Details</th>
              </tr>
            </thead>
            <tbody>
              {logs.map(log => (
                <tr key={log.filename}>
                  <td style={{ ...styles.td, whiteSpace: 'nowrap' }}>{formatTime(log.timestamp)}</td>
                  <td style={styles.td} title={log.chat_id}>{chatLabel(log.display_name, log.chat_id)}</td>
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

          {logs.length === 0 && !loading && (
            <div style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '12px' }}>No logs yet.</div>
          )}

          {totalPages > 1 && (
            <div style={styles.pagination}>
              <button style={styles.btnSecondary} disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Page {page} of {totalPages}</span>
              <button style={styles.btnSecondary} disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</button>
            </div>
          )}
        </div>
      </div>

      {selected && renderDetail()}
    </div>
  );
}
