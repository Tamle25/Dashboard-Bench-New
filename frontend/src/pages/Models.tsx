import { useState } from "react";
import { api, errMsg } from "../api";
import { MODEL_STATUS, ROLE_TEXT } from "../format";
import { useApi } from "../hooks";
import type { ModelAuth, ModelEndpoint, ModelInput, Role } from "../types";
import { Badge, ErrorNotice, ICONS, Icon, Loading, PageHead } from "../ui";

const COLS = "minmax(0, 1.1fr) 112px minmax(0, 0.9fr) minmax(0, 1.4fr) 132px 72px 220px";
const EMPTY: ModelInput = { id: "", note: "", role: "contestant", kind: "openai", base_url: "", model_name: "", secret_name: "", agent: "", auth: "api_key" };
// Gói Claude Pro/Max (OAuth) thay API key: Harbor chỉ hỗ trợ cho agent này.
const OAUTH_AGENT = "claude-code";
const OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN";
const KIND_TEXT: Record<ModelEndpoint["kind"], string> = { openai: "OpenAI-compatible", mock: "Mock (thử nghiệm)", harbor: "Agent Harbor" };

export default function ModelsPage() {
  const models = useApi(api.models, []);
  const [busyRows, setBusyRows] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<ModelInput>(EMPTY);
  // id của mô hình đang sửa; null = form đang ở chế độ thêm mới.
  const [editId, setEditId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const set = (p: Partial<ModelInput>) => setForm((f) => ({ ...f, ...p }));

  const replace = (m: ModelEndpoint) => models.setData((list) => (list || []).map((x) => (x.id === m.id ? m : x)));
  const mark = (id: string, on: boolean) =>
    setBusyRows((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });

  async function ping(id: string) {
    mark(id, true);
    try {
      replace(await api.pingModel(id));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      mark(id, false);
    }
  }

  function startEdit(m: ModelEndpoint) {
    setError(undefined);
    setNotice(undefined);
    setEditId(m.id);
    setForm({
      id: m.id, note: m.note, role: m.role, kind: m.kind, base_url: m.base_url,
      model_name: m.model_name ?? "", secret_name: m.secret_name ?? "", agent: m.agent ?? "", auth: m.auth,
    });
    document.getElementById("addm-h")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function cancelEdit() {
    setEditId(null);
    setForm(EMPTY);
    setError(undefined);
  }

  async function remove(m: ModelEndpoint) {
    const msg = m.used
      ? `Xoá mô hình ${m.id}?\n\nMô hình đã có trong lịch sử chạy. Kết quả cũ vẫn được giữ nguyên trên bảng xếp hạng, nhưng mô hình sẽ không còn chọn được cho lượt chạy mới.`
      : `Xoá mô hình ${m.id}?`;
    if (!window.confirm(msg)) return;
    mark(m.id, true);
    setError(undefined);
    try {
      await api.deleteModel(m.id, !!m.used);
      models.setData((list) => (list || []).filter((x) => x.id !== m.id));
      setNotice(`Đã xoá ${m.id}.`);
      if (editId === m.id) cancelEdit();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      mark(m.id, false);
    }
  }

  // Ô "Xác thực" chỉ hiện với agent claude-code; đổi agent khác thì OAuth tự tắt.
  const oauthAvailable = form.kind === "harbor" && form.agent?.trim() === OAUTH_AGENT;
  const oauthOn = oauthAvailable && form.auth === "oauth";

  function setAuth(auth: ModelAuth) {
    // Tên bí mật mặc định của OAuth không phải API key: bỏ đi khi quay về API key, kẻo
    // Harbor nhận token gói đăng ký như một API key.
    set({ auth, secret_name: auth === "api_key" && form.secret_name === OAUTH_ENV ? "" : form.secret_name });
  }

  async function save() {
    setSaving(true);
    setError(undefined);
    setNotice(undefined);
    // Mỗi kiểu kết nối chỉ dùng một phần của form. Gửi null cho phần không dùng để
    // không mang theo giá trị cũ khi người dùng đổi kiểu giữa chừng.
    const body = {
      role: form.role,
      kind: form.kind,
      note: form.note ?? "",
      base_url: form.kind === "harbor" ? "" : form.base_url.trim(),
      model_name: form.kind === "mock" ? null : form.model_name?.trim() || null,
      secret_name: form.kind === "mock" ? null : form.secret_name?.trim() || null,
      agent: form.kind === "mock" ? null : form.agent?.trim() || null,
      auth: oauthOn ? "oauth" as const : "api_key" as const,
    };
    try {
      if (editId) {
        replace(await api.updateModel(editId, body));
        setNotice(`Đã lưu ${editId}.`);
      } else {
        const m = await api.createModel({ ...body, id: form.id.trim() });
        models.setData((list) => [...(list || []), m].sort((a, b) => a.id.localeCompare(b.id)));
        setNotice(`Đã thêm ${m.id}.`);
      }
      cancelEdit();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  const canSave = !saving && (!!editId || !!form.id.trim()) && (
    form.kind === "mock" || (form.kind === "harbor" ? !!form.agent?.trim() : !!form.base_url.trim())
  );

  return (
    <div className="page">
      <PageHead title="Thí sinh & giám khảo"
        lead="Đăng ký endpoint của từng mô hình. Mô hình có vai trò Thí sinh sẽ hiện ở bước 2, vai trò Giám khảo hiện ở bước 3 của màn Chạy mới." />

      <section aria-labelledby="mdl-h" className="card flush-bottom">
        <h2 id="mdl-h" className="card-title">Mô hình đã đăng ký</h2>
        {models.error && <ErrorNotice error={models.error} retry={models.reload} />}
        <div className="table-scroll">
          <div style={{ minWidth: 1100 }}>
            <div className="trow thead" style={{ gridTemplateColumns: COLS }}>
              <span>Tên</span><span>Vai trò</span><span>Kết nối</span><span>Endpoint · bí mật</span><span>Trạng thái</span><span className="right">Độ trễ</span><span />
            </div>
            {models.loading && !models.data && <Loading />}
            {models.data?.length === 0 && <p className="muted" style={{ margin: 12 }}>Chưa có mô hình nào. Thêm ở bên dưới.</p>}
            {models.data?.map((m) => {
              const busy = busyRows.has(m.id);
              const st = MODEL_STATUS[busy ? "checking" : m.status];
              return (
                <div key={m.id} className={`trow ${editId === m.id ? "selected" : ""}`} style={{ gridTemplateColumns: COLS, minHeight: 56, padding: "6px 12px" }}>
                  <span style={{ minWidth: 0 }}>
                    <span className="mono" style={{ display: "block", fontWeight: 600, fontSize: 13 }}>{m.id}</span>
                    <span className="muted" style={{ display: "block", fontSize: 12 }}>{m.note}</span>
                  </span>
                  <span><span className="tag">{ROLE_TEXT[m.role]}</span></span>
                  <span style={{ fontSize: 13, color: "var(--text-2)" }}>
                    {KIND_TEXT[m.kind]}
                    {(m.kind === "harbor" || m.agent) && <span className="mono muted" style={{ display: "block", fontSize: 12 }}>agent {m.agent}</span>}
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <span className="mono ellipsis" style={{ display: "block", fontSize: 12.5 }} title={m.base_url}>
                      {m.kind === "harbor" ? (m.model_name ? `model ${m.model_name}` : "agent tự chọn model") : m.base_url}
                    </span>
                    <span className="mono" style={{ display: "block", fontSize: 12, color: m.secret_present === false ? "var(--err)" : "var(--muted)" }}>
                      {m.secret_name
                        ? `${m.auth === "oauth" ? "gói Claude (OAuth)" : "bí mật"}: ${m.secret_name}${m.secret_present === false ? " (chưa đặt)" : ""}`
                        : "không cần API key"}
                    </span>
                  </span>
                  <span title={m.last_error || undefined} style={{ minWidth: 0 }}>
                    <Badge text={st.text} tone={st.tone} dot />
                    {m.status === "down" && m.last_error && <span style={{ display: "block", fontSize: 12, color: "var(--err)", marginTop: 2 }}>{m.last_error}</span>}
                  </span>
                  <span className="right num" style={{ fontSize: 13, color: "var(--text-2)" }}>{m.latency_ms != null ? `${m.latency_ms} ms` : "—"}</span>
                  <span style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                    <button type="button" className="btn btn-sm" disabled={busy} onClick={() => ping(m.id)} aria-label={`Kiểm tra kết nối ${m.id}`}>
                      {busy ? "Đang thử…" : "Kiểm tra"}
                    </button>
                    <button type="button" className="btn btn-sm" disabled={busy} onClick={() => startEdit(m)} aria-label={`Sửa ${m.id}`}>
                      <Icon d={ICONS.edit} size={15} />Sửa
                    </button>
                    <button type="button" className="btn btn-sm btn-danger" disabled={busy} onClick={() => remove(m)} aria-label={`Xoá ${m.id}`}>
                      <Icon d={ICONS.trash} size={15} color="var(--err)" />
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {notice && <p role="status" className="notice ok">{notice}</p>}

      <section aria-labelledby="addm-h" className={`card stack ${editId ? "selected" : ""}`} style={{ gap: 16 }}>
        <h2 id="addm-h" className="card-title">{editId ? `Sửa mô hình ${editId}` : "Thêm mô hình"}</h2>
        <div className="fields">
          <label className="field">Tên hiển thị
            <input className="input mono" value={form.id} readOnly={!!editId} placeholder="vd. my-chat-v4" spellCheck={false} onChange={(e) => set({ id: e.target.value })} />
            {editId && <span className="hint">Tên không đổi được: kết quả đã chạy tham chiếu tới nó.</span>}
          </label>
          <label className="field">Vai trò
            <select className="input" value={form.role} onChange={(e) => set({ role: e.target.value as Role })}>
              <option value="contestant">Thí sinh</option>
              <option value="judge">Giám khảo</option>
              <option value="both">Cả hai</option>
            </select>
          </label>
          <label className="field">Kiểu kết nối
            <select className="input" value={form.kind} onChange={(e) => set({ kind: e.target.value as ModelInput["kind"] })}>
              <option value="openai">OpenAI-compatible (vLLM, TGI, API…)</option>
              <option value="harbor">Agent Harbor (oracle, claude-code, codex…)</option>
              <option value="mock">Mock (thử luồng, không gọi mạng)</option>
            </select>
          </label>
          <label className="field">Ghi chú
            <input className="input" value={form.note} placeholder="vd. checkpoint 10/11" onChange={(e) => set({ note: e.target.value })} />
          </label>
          {form.kind !== "harbor" && (
            <label className="field full">{form.kind === "mock" ? "Chất lượng mock" : "Base URL"}
              <input className="input mono" value={form.base_url} spellCheck={false}
                placeholder={form.kind === "mock" ? "mock://0.8  (đúng ~80% mẫu)" : "http://host:8000/v1"} onChange={(e) => set({ base_url: e.target.value })} />
            </label>
          )}
          {form.kind !== "mock" && (
            <label className="field">{form.kind === "harbor" ? "Agent Harbor" : "Agent khi chạy bộ bench Harbor"}
              <input className="input mono" value={form.agent ?? ""} spellCheck={false}
                placeholder={form.kind === "harbor" ? "oracle, nop, claude-code, codex…" : "để trống = terminus-2"} onChange={(e) => set({ agent: e.target.value })} />
              <span className="hint">{form.kind === "harbor" ? "“oracle” chạy lời giải mẫu: dùng để kiểm tra task có hỏng không." : "Agent điều khiển terminal, gọi model qua Base URL ở trên."}</span>
            </label>
          )}
          {form.kind !== "mock" && (
            <>
              {oauthAvailable && (
                <label className="field">Xác thực
                  <select className="input" value={oauthOn ? "oauth" : "api_key"} onChange={(e) => setAuth(e.target.value as ModelAuth)}>
                    <option value="api_key">API key Anthropic</option>
                    <option value="oauth">Gói Claude Pro/Max (OAuth, không cần API key)</option>
                  </select>
                  <span className="hint">
                    {oauthOn ? "Tính vào hạn mức gói đăng ký: chạy ít task song song để không bị chặn giữa chừng." : "Tính tiền theo API."}
                  </span>
                </label>
              )}
              <label className="field">{form.kind === "harbor" ? "Model (provider/model, tuỳ chọn)" : "Tên model gửi lên API"}
                <input className="input mono" value={form.model_name ?? ""} spellCheck={false}
                  placeholder={form.kind === "harbor" ? (oauthOn ? "vd. anthropic/claude-haiku-5-5" : "vd. anthropic/claude-sonnet-5-5") : "để trống = tên hiển thị"}
                  onChange={(e) => set({ model_name: e.target.value })} />
              </label>
              <label className="field">{oauthOn ? "Tên bí mật chứa token OAuth" : "Tên bí mật chứa API key"}
                <input className="input mono" value={form.secret_name ?? ""} placeholder={oauthOn ? OAUTH_ENV : "vd. MODEL_X_API_KEY"} spellCheck={false}
                  onChange={(e) => set({ secret_name: e.target.value })} />
                <span className="hint">
                  {oauthOn
                    ? <>Chạy <code>claude setup-token</code>, ghi <code>{OAUTH_ENV}=…</code> vào backend/.env. Để trống = {OAUTH_ENV}.</>
                    : "Đặt giá trị trong backend/.env hoặc biến môi trường."}
                </span>
              </label>
            </>
          )}
        </div>
        {error && <p role="alert" className="notice err">{error}</p>}
        <div className="row">
          <button type="button" className="btn btn-primary" disabled={!canSave} onClick={save}>
            {saving ? "Đang kiểm tra…" : editId ? "Lưu & kiểm tra" : "Thêm & kiểm tra"}
          </button>
          {editId && <button type="button" className="btn btn-ghost" onClick={cancelEdit}>Huỷ</button>}
          <span className="muted" style={{ fontSize: 12 }}>
            Mô hình được lưu trước, rồi hệ thống thử kết nối (OpenAI-compatible: một yêu cầu 1 token; agent Harbor: kiểm Harbor CLI, Docker và tên agent).
            Kết nối hỏng vẫn lưu — sửa lại ở nút Sửa. API key không lưu trong trình duyệt.
          </span>
        </div>
      </section>
    </div>
  );
}
