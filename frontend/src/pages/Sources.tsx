import { useState } from "react";
import { api, errMsg } from "../api";
import { AUTH_TEXT, fmtWhen, SOURCE_STATUS, SYNC_TEXT } from "../format";
import { useApi } from "../hooks";
import type { Auth, Source, SourceInput, SourceTest, SyncMode } from "../types";
import { Badge, ErrorNotice, ICONS, Icon, Kpi, Loading, PageHead } from "../ui";

const MANIFEST_EXAMPLE = `# bench.yaml — đặt ở gốc repo bench
version: 1
benches:
  - id: vnqa
    name: VN-QA
    task: Hỏi đáp kiến thức tiếng Việt
    data: data/vnqa.jsonl      # mỗi dòng: id, input, reference, category
    scoring:
      type: exact_match        # exact_match | unit_test | llm_judge | custom
    judge_required: false

  - id: sum
    name: Tóm tắt văn bản
    data: data/sum.jsonl
    scoring:
      type: llm_judge
      rubric: rubrics/sum_v1.md
    judge_required: true

  # Bộ bench dạng Harbor / Terminal-Bench
  - id: vn-layer-a-vi
    name: VN Lớp A · có dấu
    data: vn-bench/layer-a/build/vi   # thư mục chứa task
    prepare: python vn-bench/layer-a/build_variants.py --variants vi
    scoring:
      type: harbor                   # điểm = reward của verifier`;

const HOW = [
  { title: "Clone theo ref, ghim commit", body: "Mỗi lần đồng bộ, máy chủ lấy nhánh/tag đã chọn và ghi lại commit SHA hiện tại." },
  { title: "Đọc và kiểm tra manifest", body: "Mỗi bộ bench trong manifest được kiểm tra schema; bộ nào sai sẽ bị bỏ qua và báo cảnh báo." },
  { title: "Lượt chạy dùng đúng commit đã ghim", body: "Kết quả lưu kèm repo@SHA để có thể tái lập và so sánh công bằng giữa các lượt." },
  { title: "Tự đồng bộ khi có push", body: "Webhook từ máy chủ Git gọi /api/webhooks/git/:id; bộ bench mới xuất hiện ngay ở màn Chạy mới." },
];

interface FormState extends SourceInput {
  open: boolean;
  editId: number | null;
  secret: string;
}

const EMPTY: FormState = { open: false, editId: null, url: "", ref: "main", manifest_path: "bench.yaml", auth: "public", secret: "", sync_mode: "webhook" };

export default function SourcesPage() {
  const sources = useApi(api.sources, [], (d) => (d?.some((s) => s.status === "syncing" || s.status === "pending") ? 1500 : 15000));
  const [form, setForm] = useState<FormState>(EMPTY);
  const [test, setTest] = useState<SourceTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [listError, setListError] = useState<string>();

  const setF = (patch: Partial<FormState>) => {
    setForm((f) => ({ ...f, ...patch }));
    setTest(null); // đổi cấu hình thì phải kiểm tra lại
  };
  const close = () => { setForm(EMPTY); setTest(null); setFormError(undefined); };
  const payload = (): SourceInput => ({
    url: form.url.trim(), ref: form.ref.trim(), manifest_path: form.manifest_path.trim(), auth: form.auth,
    sync_mode: form.sync_mode, secret: form.auth === "public" ? undefined : form.secret || undefined,
  });

  async function runTest() {
    setTesting(true);
    setFormError(undefined);
    try {
      setTest(await api.testSource(payload()));
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setTesting(false);
    }
  }

  async function save() {
    setSaving(true);
    setFormError(undefined);
    try {
      const p = payload();
      const s = form.editId ? await api.updateSource(form.editId, p) : await api.createSource(p);
      setNotice(`${form.editId ? "Đã kết nối lại" : "Đã thêm nguồn"} ${s.repo}. Đang đồng bộ lần đầu — bộ bench sẽ có ở màn Chạy mới sau khi xong.`);
      close();
      void sources.reload();
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  async function sync(s: Source) {
    setNotice(undefined);
    setListError(undefined);
    try {
      await api.syncSource(s.id);
      void sources.reload();
    } catch (e) {
      setListError(`Không đồng bộ được ${s.repo}: ${errMsg(e)}`);
    }
  }

  async function remove(s: Source) {
    setNotice(undefined);
    setListError(undefined);
    const n = s.benches.length;
    if (!window.confirm(`Gỡ kết nối repo ${s.repo}?\n\n${n} bộ bench của repo này sẽ biến mất khỏi màn Chạy mới. Token/khoá đã lưu cũng bị xoá.`)) return;
    try {
      await api.deleteSource(s.id);
      if (form.editId === s.id) close();
      setNotice(`Đã gỡ kết nối ${s.repo}.`);
      void sources.reload();
    } catch (e) {
      // 409 khi repo đã có lượt chạy: giữ nguyên để lịch sử kết quả không bị gãy.
      setListError(errMsg(e));
    }
  }

  const edit = (s: Source) => {
    setNotice(undefined);
    setListError(undefined);
    setTest(null);
    setForm({ open: true, editId: s.id, url: s.url, ref: s.ref, manifest_path: s.manifest_path, auth: s.auth, secret: "", sync_mode: s.sync_mode });
    document.getElementById("cn-h")?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const list = sources.data || [];
  const benchCount = list.reduce((a, s) => a + s.benches.length, 0);
  const errors = list.filter((s) => s.status === "error").length;
  const warns = list.filter((s) => s.status === "warn").length;
  const needsSecret = form.auth !== "public";

  return (
    <div className="page">
      <PageHead title="Nguồn bench" lead="Mỗi bộ bench nằm trong một repo riêng. Kết nối repo, hệ thống đọc file manifest để tìm bộ bench và ghim commit cho từng lượt chạy.">
        <button type="button" className="btn btn-primary" aria-expanded={form.open} onClick={() => (form.open ? close() : setForm({ ...EMPTY, open: true }))}>
          <Icon d={ICONS.plus} size={16} color="#fff" width={2.4} />Kết nối repo
        </button>
      </PageHead>

      <div className="kpis">
        <Kpi label="Repo đã kết nối" value={list.length} sub={`${list.length - errors} đang hoạt động`} />
        <Kpi label="Bộ bench khả dụng" value={benchCount} sub="đọc từ manifest các repo" />
        <Kpi label="Cần xử lý" value={errors + warns} color={errors ? "var(--err)" : undefined} sub={`${errors} lỗi kết nối · ${warns} cảnh báo manifest`} />
      </div>

      {form.open && (
        <section aria-labelledby="cn-h" className="card stack selected" style={{ gap: 16 }}>
          <div className="card-head" style={{ alignItems: "baseline" }}>
            <h2 id="cn-h" className="card-title">{form.editId ? "Kết nối lại repo" : "Kết nối repo bench mới"}</h2>
            <span className="muted" style={{ fontSize: 13 }}>Hỗ trợ mọi máy chủ Git qua HTTPS hoặc SSH</span>
          </div>
          <div className="fields" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
            <label className="field full">URL repo
              <input className="input mono" value={form.url} readOnly={!!form.editId} spellCheck={false}
                placeholder="https://github.com/your-org/your-bench.git" onChange={(e) => setF({ url: e.target.value })} />
            </label>
            <label className="field">Nhánh / tag / commit
              <input className="input mono" value={form.ref} spellCheck={false} onChange={(e) => setF({ ref: e.target.value })} />
            </label>
            <label className="field">Đường dẫn manifest
              <input className="input mono" value={form.manifest_path} spellCheck={false} onChange={(e) => setF({ manifest_path: e.target.value })} />
            </label>
            <label className="field">Xác thực
              <select className="input" value={form.auth} onChange={(e) => setF({ auth: e.target.value as Auth })}>
                <option value="public">Công khai (không cần)</option>
                <option value="deploy_key">Deploy key (SSH, chỉ đọc)</option>
                <option value="token">Personal access token</option>
              </select>
            </label>
            {needsSecret && (
              <label className="field">{form.auth === "deploy_key" ? "Khoá riêng SSH (deploy key)" : "Personal access token (quyền đọc repo)"}
                <input className="input" type="password" autoComplete="off" value={form.secret}
                  placeholder={form.editId ? "Để trống = giữ giá trị cũ" : "Dán vào đây"} onChange={(e) => setF({ secret: e.target.value })} />
                <span className="hint">Lưu vào kho bí mật phía máy chủ, không hiển thị lại.</span>
              </label>
            )}
            <label className="field">Tự đồng bộ
              <select className="input" value={form.sync_mode} onChange={(e) => setF({ sync_mode: e.target.value as SyncMode })}>
                <option value="webhook">Khi có push (webhook)</option>
                <option value="hourly">Mỗi giờ</option>
                <option value="manual">Chỉ khi bấm Đồng bộ</option>
              </select>
            </label>
          </div>

          {test && (
            <ul role="status" aria-label="Kết quả kiểm tra kết nối" style={{ margin: 0, padding: "12px 14px", listStyle: "none", background: "var(--bg)", borderRadius: 8, display: "flex", flexDirection: "column", gap: 8 }}>
              {test.checks.map((c) => (
                <li key={c.label} style={{ display: "flex", gap: 10, fontSize: 13, alignItems: "flex-start" }}>
                  <Icon d={c.ok ? ICONS.ok : ICONS.fail} color={c.ok ? "var(--ok)" : "var(--err)"} width={2.4} />
                  <span><strong style={{ fontWeight: 600 }}>{c.label}</strong>{c.detail && <span style={{ color: c.ok ? "var(--muted)" : "var(--err)" }}> — {c.detail}</span>}</span>
                </li>
              ))}
              {test.benches.length > 0 && (
                <li className="chips" style={{ gap: 6, paddingLeft: 28 }}>
                  {test.benches.map((b) => (
                    <span key={b.key} className="tag">
                      {b.name} · {b.size == null ? "sinh khi đồng bộ" : `${b.size} ${b.harbor ? "task" : "mẫu"}`}
                    </span>
                  ))}
                </li>
              )}
              {test.warnings.map((w) => <li key={w} className="notice warn">{w}</li>)}
            </ul>
          )}
          {formError && <p role="alert" className="notice err">{formError}</p>}

          <div className="row">
            <button type="button" className="btn" disabled={testing || !form.url.trim()} onClick={runTest} style={{ fontWeight: 600 }}>
              {testing ? "Đang kiểm tra…" : "Kiểm tra kết nối"}
            </button>
            <button type="button" className="btn btn-primary" disabled={!test?.ok || saving} onClick={save}>
              {saving ? "Đang lưu…" : form.editId ? "Lưu & đồng bộ" : "Thêm nguồn"}
            </button>
            <button type="button" className="btn btn-ghost" onClick={close}>Huỷ</button>
            <span className="muted" style={{ fontSize: 12 }}>
              {test?.ok
                ? "Kết nối ổn. Bấm để lưu; lần đồng bộ đầu tải toàn bộ repo, repo lớn có thể mất vài chục phút."
                : "Cần kiểm tra kết nối thành công trước khi lưu."}
            </span>
          </div>
        </section>
      )}

      {notice && <p role="status" className="notice ok">{notice}</p>}
      {listError && <p role="alert" className="notice err">{listError}</p>}

      <section aria-labelledby="src-h" className="card flush-bottom">
        <h2 id="src-h" className="card-title" style={{ marginBottom: 6 }}>Repo đã kết nối</h2>
        {sources.error && <ErrorNotice error={sources.error} retry={sources.reload} />}
        {sources.loading && !sources.data && <Loading />}
        {sources.data?.length === 0 && <p className="muted" style={{ padding: "12px 0 16px", margin: 0 }}>Chưa kết nối repo nào. Bấm “Kết nối repo” để bắt đầu.</p>}
        {list.map((s) => <SourceRow key={s.id} s={s} onSync={() => sync(s)} onFix={() => edit(s)} onDelete={() => remove(s)} />)}
      </section>

      <div className="pair">
        <section aria-labelledby="mf-h" className="log" style={{ flexBasis: 460 }}>
          <div className="log-head"><h2 id="mf-h" className="log-title">Hợp đồng manifest <span>· bench.yaml ở gốc repo</span></h2></div>
          <pre className="code" style={{ borderRadius: 0, padding: "14px 16px" }}>{MANIFEST_EXAMPLE}</pre>
        </section>
        <section aria-labelledby="how-h" className="card" style={{ flexBasis: 360 }}>
          <h2 id="how-h" className="card-title" style={{ marginBottom: 14 }}>Hệ thống dùng repo bench thế nào</h2>
          <ol style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 14 }}>
            {HOW.map((h, i) => (
              <li key={h.title} style={{ display: "flex", gap: 12 }}>
                <span className="step-num">{i + 1}</span>
                <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <strong style={{ fontWeight: 600 }}>{h.title}</strong>
                  <span className="muted" style={{ fontSize: 13 }}>{h.body}</span>
                </span>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </div>
  );
}

function SourceRow({ s, onSync, onFix, onDelete }: { s: Source; onSync: () => void; onFix: () => void; onDelete: () => void }) {
  const st = SOURCE_STATUS[s.status];
  const syncing = s.status === "syncing" || s.status === "pending";
  const isErr = s.status === "error";
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "14px 20px", alignItems: "flex-start", padding: "16px 0", borderBottom: "1px solid var(--border-soft)" }}>
      <div style={{ flex: "1 1 300px", minWidth: 0, display: "flex", gap: 12 }}>
        <span style={{ flex: "none", width: 36, height: 36, borderRadius: 8, background: "var(--tag-bg)", color: "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Icon d={ICONS.source} />
        </span>
        <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="mono" style={{ fontWeight: 600, fontSize: 13.5, overflowWrap: "anywhere" }}>{s.repo}</span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            <span className="mono">{s.ref}{s.sha ? ` @ ${s.sha.slice(0, 7)}` : ""}</span> · {s.host} · {AUTH_TEXT[s.auth]}
          </span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            Manifest <span className="mono" style={{ color: "var(--text-2)" }}>{s.manifest_path}</span> · {SYNC_TEXT[s.sync_mode]}
          </span>
        </div>
      </div>
      <div style={{ flex: "1 1 220px", minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        <span className="muted" style={{ fontSize: 12, fontWeight: 600 }}>{s.benches.length ? `${s.benches.length} bộ bench` : "Chưa có bộ bench"}</span>
        <div className="chips" style={{ gap: 6 }}>{s.benches.map((b) => <span key={b.id} className="tag">{b.name}</span>)}</div>
      </div>
      <div style={{ flex: "0 1 300px", minWidth: 0, display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 10, marginLeft: "auto" }}>
        <span style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2 }}>
          <Badge text={st.text} tone={st.tone} dot />
          <span className="muted" style={{ fontSize: 12 }}>
            {syncing ? (s.synced_at ? "đang lấy commit mới…" : "lần đầu: đang tải toàn bộ repo…") : `Đồng bộ: ${s.synced_at ? fmtWhen(s.synced_at) : "chưa lần nào"}`}
          </span>
        </span>
        <button type="button" className={`btn btn-sm ${isErr ? "btn-danger" : ""}`} onClick={onFix} aria-label={`Sửa kết nối ${s.repo}`}>
          <Icon d={ICONS.edit} size={15} color={isErr ? "var(--err)" : undefined} />{isErr ? "Cập nhật kết nối" : "Sửa"}
        </button>
        <button type="button" className="btn btn-sm" disabled={syncing} onClick={onSync} aria-label={`Đồng bộ ${s.repo}`}>
          <Icon d={ICONS.sync} size={15} />{syncing ? "Đang đồng bộ…" : "Đồng bộ"}
        </button>
        <button type="button" className="btn btn-sm btn-danger" onClick={onDelete} aria-label={`Gỡ kết nối ${s.repo}`}>
          <Icon d={ICONS.trash} size={15} color="var(--err)" />
        </button>
      </div>
      {s.message && <p className={`notice ${isErr ? "err" : "warn"}`} style={{ flex: "1 1 100%" }}>{s.message}</p>}
      {s.sync_mode === "webhook" && !isErr && (
        <p className="muted" style={{ flex: "1 1 100%", margin: 0, fontSize: 12 }}>
          Webhook: <span className="mono">{`${window.location.origin}/api/webhooks/git/${s.id}`}</span>
        </p>
      )}
    </div>
  );
}
