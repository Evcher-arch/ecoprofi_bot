export interface Session {
  token: string;
  expires_at: string;
  dialog_id: string;
  user_id: number;
  limits: { question_chars: number };
}

export interface MaterialCard {
  type: 'episode' | 'page' | 'letter' | 'document' | 'material' | 'navigation';
  material_id: number;
  title: string;
  lecture: { title: string | null; date: string | null } | null;
  explanation: string;
  url: string;
  excerpt?: string | null;
  start_ms?: number;
  end_ms?: number;
  page?: number | null;
  pages_total?: number | null;
  recognized?: boolean;
  requisites?: { date: string | null; number: string; sender: string | null } | null;
  requisites_note?: string | null;
  material_kind?: string;
}

export interface Answer {
  outcome: 'found' | 'clarify' | 'empty';
  text: string;
  cards: MaterialCard[];
}

export interface ChatRequest {
  dialog_id: string;
  request_id: string;
  sequence: number;
  state: 'queued' | 'running' | 'completed' | 'retryable_failed' | 'denied' | 'invalidated' | 'cancelled';
  operation_id: string;
  question: string;
  retry_allowed: boolean;
  answer?: Answer;
  message?: string;
  error_code?: string;
}

export interface History {
  dialog_id: string;
  items: ChatRequest[];
  next_after_sequence: number;
  has_more: boolean;
}
