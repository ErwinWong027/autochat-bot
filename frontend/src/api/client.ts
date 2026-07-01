const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {})
    },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export type Overview = {
  contact_count: number;
  message_count: number;
  reply_count: number;
  sent_count: number;
};

export type ContactSummary = {
  contact_id: string;
  thread_id?: string;
  platform?: string;
  platform_contact_id?: string;
  display_name?: string;
  identity?: string;
  profile?: string;
  relationship_stage?: string;
  recent_emotion?: string;
  interaction_frequency?: string;
  updated_at?: string;
  channel?: string;
  message_count: number;
  reply_count: number;
  last_message?: string;
  last_message_at?: string;
  memory_updated_at?: string;
};

export type ThreadSummary = {
  thread_id: string;
  platform: string;
  platform_contact_id: string;
  display_name?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
};

export type ProfileField = {
  field: string;
  value: string;
  confidence: number;
  source: string;
  updated_at: string;
};

export type Message = {
  id: number;
  conversation_id: string;
  contact_id: string;
  channel: string;
  message_id?: string;
  role: string;
  content: string;
  technique?: string;
  decision_reason?: string;
  created_at: string;
};

export type ContactDetail = {
  contact: ContactSummary & Record<string, string | number | undefined>;
  thread?: ThreadSummary;
  fields: Record<string, ProfileField>;
  evidence: Array<Record<string, string | number>>;
  conversations: Array<Record<string, string>>;
  messages: Message[];
  memory?: Record<string, unknown>;
  pending_group?: Array<Record<string, string | number>>;
  draft_cache: Array<Record<string, string>>;
  profile_text?: string;
  recent_messages_json?: string;
  last_llm_prompts?: Record<string, { prompt: string; model: string; at: string }>;
};

export type ContactsResponse = {
  summary: Overview;
  contacts: ContactSummary[];
};

export type BumbleStatus = {
  running: boolean;
  stage: string;
  status_code: number;
  last_error?: string;
  last_contact_id?: string;
  last_contact_name?: string;
  last_message?: string;
  last_draft?: string;
  sent_count?: number;
  draft_count?: number;
  contact_count?: number;
  message_count?: number;
  pending_group_count?: number;
  logs?: Array<{
    time: string;
    stage: string;
    status_code: number;
    ok: boolean;
    message: string;
    data?: Record<string, unknown>;
  }>;
};

export type DraftPayload = {
  contact_id: string;
  message: string;
  thread_id?: string;
  platform?: string;
  channel: string;
  conversation_id?: string;
  extra_context?: string;
  pending_group_context?: string;
  memory_context?: string;
  profile_context?: string;
  contact_profile?: string;
  contact_identity?: string;
  relationship_stage?: string;
  recent_emotion?: string;
  interaction_frequency?: string;
};

export type DraftResult = {
  draft: string;
  technique: string;
  decision_reason: string;
  scenario: string;
  conversation_id: string;
};

export function getContacts() {
  return request<ContactsResponse>("/contacts");
}

export function getContact(contactId: string) {
  return request<ContactDetail>(`/contacts/${encodeURIComponent(contactId)}`);
}

export function getContactDebug(contactId: string) {
  return request<{ last_llm_prompt: string }>(`/contacts/${encodeURIComponent(contactId)}/debug`);
}

export function createDraft(payload: DraftPayload) {
  return request<DraftResult>("/draft", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function getBumbleStatus() {
  return request<BumbleStatus>("/agent/bumble/status");
}

export function startBumble(payload: {
  target_url: string;
  auto_send_enabled: boolean;
  poll_seconds: number;
  refresh_profile: boolean;
}) {
  return request<BumbleStatus>("/agent/bumble/run", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function stopBumble() {
  return request<BumbleStatus>("/agent/bumble/stop", {
    method: "POST"
  });
}

export type AndroidStatus = {
  running: boolean;
  stage: string;
  status_code: number;
  app: string;
  last_error?: string;
  last_contact_id?: string;
  last_contact_name?: string;
  last_draft?: string;
  sent_count?: number;
  draft_count?: number;
  contact_count?: number;
  pending_group_count?: number;
  skipped_duplicate_count?: number;
  logs?: Array<{
    time: string;
    stage: string;
    status_code: number;
    ok: boolean;
    message: string;
    data?: Record<string, unknown>;
  }>;
};

export type AndroidRunPayload = {
  adb_address: string;
  auto_send_enabled: boolean | null;
  poll_seconds: number;
};

export function getAndroidStatus(app: string) {
  return request<AndroidStatus>(`/agent/android/${encodeURIComponent(app)}/status`);
}

export function startAndroid(app: string, payload: AndroidRunPayload) {
  return request<AndroidStatus>(`/agent/android/${encodeURIComponent(app)}/run`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function stopAndroid(app: string) {
  return request<AndroidStatus>(`/agent/android/${encodeURIComponent(app)}/stop`, {
    method: "POST"
  });
}
