import type { ModelStatus, Role, RunStatus, SourceStatus, SyncMode, Auth } from "./types";

export const fmtScore = (x: number | null | undefined) => (x == null ? "—" : x.toFixed(1).replace(".", ","));

export const fmtDelta = (x: number | null | undefined) =>
  x == null ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(1).replace(".", ",");

const pad = (n: number) => String(n).padStart(2, "0");

export function fmtWhen(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === today.toDateString() ? `Hôm nay ${time}` : `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${time}`;
}

export function fmtClock(iso: string | null | undefined) {
  if (!iso) return "--:--:--";
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtDuration(s: number | null | undefined) {
  if (s == null) return "—";
  if (s < 60) return `${s} giây`;
  if (s < 3600) return `${Math.round(s / 60)} phút`;
  return `${Math.floor(s / 3600)} giờ ${Math.round((s % 3600) / 60)} phút`;
}

export const fmtInt = (n: number) => n.toLocaleString("vi-VN");

type Tone = "ok" | "err" | "warn" | "info" | "neutral";

export const RUN_STATUS: Record<RunStatus, { text: string; tone: Tone }> = {
  queued: { text: "Đang chờ", tone: "info" },
  running: { text: "Đang chạy", tone: "info" },
  done: { text: "Hoàn tất", tone: "ok" },
  failed: { text: "Lỗi", tone: "err" },
  stopped: { text: "Đã dừng", tone: "warn" },
};

export const SOURCE_STATUS: Record<SourceStatus, { text: string; tone: Tone }> = {
  pending: { text: "Chờ đồng bộ", tone: "neutral" },
  syncing: { text: "Đang đồng bộ", tone: "info" },
  ok: { text: "Đã đồng bộ", tone: "ok" },
  warn: { text: "Có cảnh báo", tone: "warn" },
  error: { text: "Lỗi kết nối", tone: "err" },
};

export const MODEL_STATUS: Record<ModelStatus, { text: string; tone: Tone }> = {
  unknown: { text: "Chưa kiểm tra", tone: "neutral" },
  checking: { text: "Đang kiểm tra", tone: "info" },
  ok: { text: "Sẵn sàng", tone: "ok" },
  down: { text: "Không phản hồi", tone: "err" },
};

export const ROLE_TEXT: Record<Role, string> = { contestant: "Thí sinh", judge: "Giám khảo", both: "Cả hai" };
export const SYNC_TEXT: Record<SyncMode, string> = { webhook: "tự đồng bộ khi push", hourly: "đồng bộ mỗi giờ", manual: "đồng bộ thủ công" };
export const AUTH_TEXT: Record<Auth, string> = { public: "Công khai", deploy_key: "Deploy key", token: "Personal access token" };
