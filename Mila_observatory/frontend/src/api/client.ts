const API_BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`API Error ${res.status}: ${err}`);
  }
  return res.json();
}

// ─── Dashboard ──────────────────────────────────────────────

export interface StatusData {
  status: string;
  uptime: number | null;
  model: string;
  messages_total: number;
  memories_total: number;
  last_activity: string | null;
  sleep_mode: boolean;
  runtime_config_version: number;
}

export interface EventItem {
  timestamp: string;
  type: string;
  description: string;
}

export interface DashboardData {
  status: StatusData;
  recent_events: EventItem[];
}

export const getDashboard = () => request<DashboardData>('/dashboard');
export const getStatus = () => request<StatusData>('/status');
export const getEvents = (limit = 10) => request<EventItem[]>(`/events?limit=${limit}`);

// ─── Model ──────────────────────────────────────────────────

export interface ModelParams {
  model?: string;
  api_base?: string;
  api_key?: string;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  frequency_penalty?: number;
  presence_penalty?: number;
  tool_choice?: 'required' | 'auto' | 'none';
}

export interface ModelData {
  main: Record<string, unknown>;
  night: Record<string, unknown>;
}

export interface TestQueryResult {
  success: boolean;
  response: string;
  error?: string;
  timing: string;
  model: string;
  client_type: string;
}

export const getModelConfig = () => request<ModelData>('/model');
export const updateMainModel = (params: ModelParams) =>
  request<ModelData>('/model/main', { method: 'PUT', body: JSON.stringify(params) });
export const updateNightModel = (params: ModelParams) =>
  request<ModelData>('/model/night', { method: 'PUT', body: JSON.stringify(params) });
export const testModel = (prompt: string, clientType = 'main', temperature?: number, maxTokens?: number) =>
  request<TestQueryResult>('/model/test', {
    method: 'POST',
    body: JSON.stringify({ prompt, client_type: clientType, temperature, max_tokens: maxTokens }),
  });

// ─── Memory ─────────────────────────────────────────────────

export interface MemoryItem {
  mem_id: string;
  user_id: string;
  content: string;
  type: string;
  tag: string | null;
  importance: number;
  confidence: number;
  source_chat_id: string | null;
  source_message_id: string | null;
  created_at: string | null;
  last_accessed_at: string | null;
}

export interface MemoryList {
  items: MemoryItem[];
  total: number;
  page: number;
  limit: number;
}

export const getMemories = (query?: string, page = 1, limit = 50, category?: string) => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) });
  if (query) params.set('query', query);
  if (category) params.set('category', category);
  return request<MemoryList>(`/memory?${params}`);
};

export const getMemory = (id: string) => request<MemoryItem>(`/memory/${id}`);
export const updateMemory = (id: string, data: Record<string, unknown>) =>
  request<MemoryItem>(`/memory/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteMemory = (id: string) =>
  request<{ success: boolean }>(`/memory/${id}`, { method: 'DELETE' });
export const createMemory = (data: { content: string; type?: string; tag?: string; importance?: number }) =>
  request<MemoryItem>('/memory', { method: 'POST', body: JSON.stringify(data) });
export const rebuildFaiss = () =>
  request<{ success: boolean; message: string; count: number }>('/memory/rebuild-faiss', { method: 'POST' });

// ─── Tools ──────────────────────────────────────────────────

export interface ToolItem {
  name: string;
  enabled: boolean;
  description: string;
}

export const getTools = () => request<{ tools: ToolItem[] }>('/tools');
export const updateTool = (name: string, enabled: boolean) =>
  request<ToolItem>(`/tools/${name}`, { method: 'PUT', body: JSON.stringify({ enabled }) });

// ─── Persona ────────────────────────────────────────────────

export interface PersonaData {
  custom_text: string;
  parameters: Record<string, number>;
  using_custom: boolean;
}

export const getPersona = () => request<PersonaData>('/persona');
export const updatePersona = (data: { custom_text?: string; parameters?: Record<string, number> }) =>
  request<PersonaData>('/persona', { method: 'PUT', body: JSON.stringify(data) });
export const resetPersona = () => request<PersonaData>('/persona/reset', { method: 'POST' });

// ─── Pipeline ───────────────────────────────────────────────

export interface PipelineLogItem {
  filename: string;
  timestamp: string;
  user_message: string;
  success: boolean;
  error?: string;
  timing?: Record<string, string>;
  actions_count: number;
  iterations: number;
}

export interface PipelineLogList {
  items: PipelineLogItem[];
  total: number;
  page: number;
  limit: number;
}

export const getPipelineLogs = (page = 1, limit = 20, successOnly?: boolean) => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) });
  if (successOnly !== undefined) params.set('success_only', String(successOnly));
  return request<PipelineLogList>(`/pipeline/logs?${params}`);
};

export const getPipelineLog = (filename: string) =>
  request<{ filename: string; data: Record<string, unknown> }>(`/pipeline/logs/${filename}`);

// ─── Config ─────────────────────────────────────────────────

export const getConfig = () => request<{ config: Record<string, unknown> }>('/config');
export const updateConfig = (updates: Record<string, unknown>) =>
  request<{ config: Record<string, unknown> }>('/config', {
    method: 'PUT',
    body: JSON.stringify({ updates }),
  });
export const reloadConfig = () =>
  request<{ success: boolean; message: string }>('/config/reload', { method: 'POST' });
export const getRuntimeState = () => request<{ config: Record<string, unknown> }>('/runtime-state');

// ─── Sleep ────────────────────────────────────────────────────

export interface SleepStatus {
  state: string;
  session_id: string | null;
  current_stage: string;
  progress_pct: number;
  started_at: string | null;
  scheduled_end_at: string | null;
  completed_at: string | null;
  triggered_by: string;
  error_message: string | null;
  queue_count: number;
  elapsed_seconds: number | null;
  remaining_seconds: number | null;
  in_progress: boolean;
}

export interface SleepResponse {
  success: boolean;
  message: string;
  session_id: string | null;
  duration_minutes: number | null;
}

export const getSleepStatus = () => request<SleepStatus>('/sleep/status');
export const startSleep = (duration_minutes?: number) =>
  request<SleepResponse>('/sleep/start', {
    method: 'POST',
    body: JSON.stringify({ duration_minutes, triggered_by: 'manual' }),
  });
export const wakeUp = () =>
  request<SleepResponse>('/sleep/wake', { method: 'POST' });
export const getQueueCount = () => request<{ count: number }>('/sleep/queue-count');
export const updateSleepConfig = (data: Record<string, unknown>) =>
  request<{ success: boolean; message: string }>('/sleep/config', {
    method: 'POST',
    body: JSON.stringify(data),
  });

// ─── Sleep Estimate ─────────────────────────────────────────

export interface SleepEstimate {
  diary_entries: number;
  active_dialogs: number;
  total_messages: number;
  estimated_minutes: number;
  breakdown: {
    diary_analysis_sec: number;
    dialog_summarization_sec: number;
    fact_extraction_sec: number;
    overhead_sec: number;
    total_sec: number;
  };
}

export interface SleepConfigData {
  start_hour: number | null;
  end_hour: number | null;
  forced_awake: boolean;
  save_processed: boolean;
}

export const getSleepEstimate = () => request<SleepEstimate>('/sleep/estimate');
export const getSleepConfig = () => request<SleepConfigData>('/sleep/config');

// ─── State Agent ────────────────────────────────────────────

export interface StateAgentModelInfo {
  loaded: boolean;
  error: string | null;
  model_path: string;
  num_features: number;
  actions: string[];
}

export interface StateAgentNeeds {
  seconds_since_last_msg: number;
  current_hour: number;
  fatigue: number;
  need_social: number;
  boredom: number;
  in_active_dialog: boolean;
  timestamp: string;
}

export interface StateAgentInference {
  action: string;
  probabilities: Record<string, number>;
  inference_time_ms: number;
  model_loaded: boolean;
  fallback: boolean;
}

export interface StateAgentStatus {
  model: StateAgentModelInfo;
  needs: StateAgentNeeds;
  inference: StateAgentInference | null;
}

export interface NeedsOverride {
  fatigue?: number;
  need_social?: number;
  boredom?: number;
  seconds_since_last_msg?: number;
  current_hour?: number;
  in_active_dialog?: boolean;
}

export const getStateAgentStatus = () => request<StateAgentStatus>('/state-agent');
export const runStateAgent = () => request<StateAgentInference>('/state-agent/run', { method: 'POST' });
export const overrideNeeds = (data: NeedsOverride) =>
  request<StateAgentStatus>('/state-agent/needs', { method: 'POST', body: JSON.stringify(data) });
