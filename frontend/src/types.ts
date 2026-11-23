// Kiểu dữ liệu khớp với phản hồi của backend FastAPI (backend/app/routers/*).

export type RunStatus = "queued" | "running" | "done" | "failed" | "stopped";
export type SourceStatus = "pending" | "syncing" | "ok" | "warn" | "error";
export type ModelStatus = "unknown" | "checking" | "ok" | "down";
export type Auth = "public" | "deploy_key" | "token";
export type SyncMode = "webhook" | "hourly" | "manual";
export type Role = "contestant" | "judge" | "both";

export interface Bench {
  id: number;
  key: string;
  name: string;
  task: string;
  scoring_type: string;
  scoring: string;
  judge_required: boolean;
  rubric_path: string | null;
  size: number;
  source: string;
  source_status: SourceStatus;
  data_path: string;
  prepare: string[];
}

export type ModelKind = "openai" | "mock" | "harbor";

export interface Source {
  id: number;
  repo: string;
  url: string;
  host: string;
  ref: string;
  sha: string | null;
  manifest_path: string;
  auth: Auth;
  has_secret: boolean;
  sync_mode: SyncMode;
  status: SourceStatus;
  message: string | null;
  synced_at: string | null;
  benches: { id: number; key: string; name: string; scoring: string; size: number }[];
}

export interface SourceInput {
  url: string;
  ref: string;
  manifest_path: string;
  auth: Auth;
  secret?: string;
  sync_mode: SyncMode;
}

export interface Check {
  label: string;
  detail: string;
  ok: boolean;
}

export interface SourceTest {
  ok: boolean;
  sha: string | null;
  checks: Check[];
  benches: { key: string; name: string; size: number | null; prepare: boolean; harbor: boolean }[];
  warnings: string[];
}

export interface ModelEndpoint {
  id: string;
  note: string;
  role: Role;
  kind: ModelKind;
  base_url: string;
  model_name: string | null;
  agent: string | null;
  secret_name: string | null;
  secret_present: boolean | null;
  // oauth: bí mật là token gói Claude Pro/Max thay cho API key (chỉ agent claude-code).
  auth: ModelAuth;
  status: ModelStatus;
  latency_ms: number | null;
  last_error: string | null;
  checked_at: string | null;
  // Đã xuất hiện trong lượt chạy nào chưa (thí sinh hoặc giám khảo). Xoá mô hình như vậy
  // cần xác nhận thêm vì nó còn hiện trên bảng xếp hạng.
  used: boolean | null;
}

export interface ModelInput {
  id: string;
  note?: string;
  role: Role;
  kind: ModelKind;
  base_url: string;
  model_name?: string | null;
  secret_name?: string | null;
  agent?: string | null;
  auth?: ModelAuth;
}

export type ModelAuth = "api_key" | "oauth";

export interface RunParams {
  temperature: number;
  max_tokens: number;
  samples: number | "all";
  concurrency: number;
  seed: number;
}

export interface RunInput extends RunParams {
  bench_id: number;
  models: string[];
  judge_id: string | null;
  threshold: number;
}

export interface RunEntry {
  id: number;
  model: string;
  total: number;
  done: number;
  passed: number;
  review: number;
  score: number | null;
  status: RunStatus;
  error: string | null;
}

export interface LogLine {
  id: number;
  t: string;
  lvl: "INFO" | "WARN" | "ERROR" | "DONE";
  msg: string;
}

export interface Run {
  id: number;
  status: RunStatus;
  bench: { id: number; name: string; scoring: string; judge_required: boolean };
  source: string;
  judge: string | null;
  threshold: number;
  params: RunParams;
  warns: number;
  errs: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  entries: RunEntry[];
  logs?: LogLine[];
}

export interface ResultRow {
  id: number;
  run_id: number;
  bench: { id: number; name: string };
  model: string;
  judge: string | null;
  score: number | null;
  delta: number | null;
  status: RunStatus;
  when: string | null;
}

export interface LeaderRow {
  entry_id: number;
  model: string;
  score: number;
  judge: string | null;
  when: string | null;
}

export interface ResultDetail extends Omit<ResultRow, "bench"> {
  bench: { id: number; name: string; scoring: string; judge_required: boolean; rubric_path: string | null };
  source: string;
  params: RunParams;
  threshold: number;
  total: number;
  done: number;
  passed: number;
  review: number;
  error: string | null;
  error_logs: { lvl: string; msg: string }[];
  duration_s: number | null;
  previous: { id: number; score: number | null } | null;
  categories: { name: string; score: number }[];
  comparison: LeaderRow[];
}

export interface SampleRow {
  id: number;
  sample_id: string;
  category: string;
  prompt: string;
  output: string;
  reference: string;
  raw_score: string;
  passed: boolean;
  note: string;
  needs_review: boolean;
  latency_ms: number;
  tokens: number;
}

export interface SamplePage {
  counts: { all: number; pass: number; fail: number };
  items: SampleRow[];
}

export interface Stats {
  entries: number;
  entries_done: number;
  entries_failed: number;
  models_evaluated: number;
  benches: number;
  benches_with_judge: number;
  sources: number;
  sources_ok: number;
  sources_error: number;
  sources_warn: number;
  models_down: number;
  active_run: { id: number; pct: number } | null;
}
