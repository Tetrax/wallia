export interface User {
  id: string;
  email: string;
  is_admin: boolean;
  created_at: string | null;
  password_changed_at: string | null;
}

export interface CaseItem {
  id: string;
  text: string;
  status: "proposed" | "confirmed" | "refuted" | "missing";
  origin: "user_message" | "user_explicit" | "assistant" | "unknown";
  message_id: string | null;
  created_at: string;
}

export interface CaseState {
  product: string | null;
  version: string | null;
  symptom: string | null;
  facts: CaseItem[];
  hypotheses: CaseItem[];
  proposed_checks: CaseItem[];
  performed_checks: CaseItem[];
  results: CaseItem[];
  missing_info: CaseItem[];
}

export interface Source {
  /** Discriminant de provenance ; absent = corpus (snapshots antérieurs). */
  source_type?: "corpus" | "web";
  chunk_id: string | null;
  document_id: string | null;
  title: string;
  product: string | null;
  versions: string[];
  demo: boolean;
  scope: string;
  language: string | null;
  page_start: number | null;
  page_end: number | null;
  section: string | null;
  kind: string;
  text: string;
  /** Logit brut du reclassement (classement réel) — jamais une probabilité ;
   *  null pour une source web (aucun score factice). */
  score: number | null;
  score_kind?: string | null;
  score_vector: number | null;
  score_text: number | null;
  /** Disponibilité résolue du document cité (false = document supprimé). */
  available?: boolean;
  /** Source web uniquement : URL publique HTTPS + domaine + état de version. */
  url?: string | null;
  domain?: string | null;
  version_state?: string | null;
}

export interface RetrievalPayload {
  status: "ok" | "no_relevant_source" | "empty_corpus" | "embeddings_unavailable" | "retrieval_unavailable";
  sources: Source[];
  diagnostics: Record<string, unknown>;
  /** Métadonnée opérationnelle du repli web — absente des anciens flux. */
  web?: WebFallbackMeta;
}

/** Statut du repli web pour un tour : état opérationnel, jamais une preuve
 *  d'appel ; la barre globale reste l'état du connecteur. */
export interface WebFallbackMeta {
  status: "not_needed" | "disabled" | "unavailable" | "no_results" | "ok";
  reason: string | null;
  /** Requête réellement envoyée — vocabulaire fermé public uniquement. */
  query: string | null;
}

export interface RerankerPayload {
  backend: string;
  model: string;
  revision: string;
  weights_sha256?: string;
  threshold?: number;
  max_length?: number;
  batch_size?: number;
  state?: string;
  error_type?: string | null;
  note?: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  status: "streaming" | "complete" | "cancelled" | "error" | "interrupted";
  error: string | null;
  model: string | null;
  demo: boolean;
  sources: Source[] | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Attachment {
  id: string;
  conversation_id: string;
  message_id: string | null;
  filename: string;
  content_type: string;
  kind: "pdf" | "text" | "image";
  size_bytes: number;
  pages: number | null;
  width: number | null;
  height: number | null;
  has_extracted_text: boolean;
  created_at: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  case_state: CaseState;
  created_at: string | null;
  updated_at: string | null;
  message_count?: number;
  messages?: Message[];
  attachments?: Attachment[];
  last_message?: { role: string; excerpt: string; status: string; created_at: string | null };
}

export interface Job {
  id: string;
  document_id: string | null;
  document_title?: string | null;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  manual_retries: number;
  progress: Record<string, unknown>;
  error: string | null;
  available_at: string | null;
  lease_until: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface DocumentItem {
  id: string;
  title: string;
  origin: string;
  product: string | null;
  versions: string[];
  language: string;
  document_date: string | null;
  checksum_sha256: string;
  demo: boolean;
  scope: "demo" | "official";
  status: "queued" | "processing" | "ready" | "failed" | "deleting";
  current_generation: number;
  embedding_model: string | null;
  embedding_revision: string | null;
  embedding_dim: number | null;
  original_filename: string;
  content_type: string;
  size_bytes: number | null;
  page_count: number | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  chunks_current?: number;
  last_job?: Job;
}

export interface StatusPayload {
  app: { version: string; git_sha: string | null; env: string; demo_mode: boolean };
  user: User;
  embedding: { backend: string; model: string; revision: string; dim: number; note?: string };
  provider: {
    endpoint: string;
    model: string;
    key_configured: boolean;
    vision_enabled: boolean;
    vision_effective?: boolean;
    last_test: Record<string, unknown> | null;
  };
  web: { available: boolean; reason: string | null };
  vision: { available: boolean; reason: string | null };
  retrieval: { top_k: number; reranker: RerankerPayload };
  corpus: { documents_total: number; documents_by_status: Record<string, number>; chunks_serving: number };
  jobs: {
    by_status: Record<string, number>;
    worker: { alive: boolean; heartbeat?: Record<string, unknown> };
  };
}

export interface SettingsPayload {
  provider: {
    endpoint: string;
    model: string;
    timeout_s: number;
    key_configured: boolean;
    vision_enabled: boolean;
    allowed_domains: string[];
  };
  retrieval: { top_k: number };
  last_provider_test: Record<string, unknown> | null;
  env: string;
}

export interface ChunkItem {
  id: string;
  seq: number;
  kind: string;
  section: string | null;
  page_start: number | null;
  page_end: number | null;
  token_count: number | null;
  text: string;
}
