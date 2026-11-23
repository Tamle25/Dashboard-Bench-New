import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, errMsg } from "../api";
import { fmtInt } from "../format";
import { useApi } from "../hooks";
import type { RunInput } from "../types";
import { ErrorNotice, ICONS, Icon, Loading, PageHead } from "../ui";

type Form = Omit<RunInput, "samples" | "bench_id"> & { bench_id: number | null; samples: string };

const DEFAULT: Form = {
  bench_id: null, models: [], judge_id: null, threshold: 6,
  temperature: 0.2, max_tokens: 1024, samples: "40", concurrency: 8, seed: 42,
};

function Step({ n, id, title, aside }: { n: number; id: string; title: string; aside?: ReactNode }) {
  return (
    <div className="card-head">
      <div className="step"><span className="step-num">{n}</span><h2 id={id} className="card-title">{title}</h2></div>
      {aside}
    </div>
  );
}

export default function NewRunPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const prefill = (location.state as Partial<Form> | null) || null;
  const benches = useApi(api.benches, []);
  const models = useApi(api.models, []);
  const active = useApi(api.activeRun, [], 5000);
  const [form, setForm] = useState<Form>({ ...DEFAULT, ...(prefill || {}) });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>();
  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));

  const contestants = useMemo(() => (models.data || []).filter((m) => m.role !== "judge"), [models.data]);
  // Giám khảo chấm theo rubric qua Chat Completions, nên agent Harbor không làm giám khảo được
  // (backend cũng từ chối). Loại khỏi danh sách thay vì để chọn rồi hỏng giữa lượt chạy.
  const judges = useMemo(() => (models.data || []).filter((m) => m.role !== "contestant" && m.kind !== "harbor"), [models.data]);

  // Mặc định: bộ bench đầu tiên, hai thí sinh sẵn sàng đầu tiên, giám khảo sẵn sàng đầu tiên.
  useEffect(() => {
    if (form.bench_id == null && benches.data?.length) set({ bench_id: benches.data[0].id });
  }, [benches.data]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!models.data) return;
    if (!prefill?.models && form.models.length === 0) set({ models: contestants.filter((m) => m.status !== "down").slice(0, 2).map((m) => m.id) });
    if (!form.judge_id) set({ judge_id: (judges.find((m) => m.status !== "down") || judges[0])?.id ?? null });
  }, [models.data]); // eslint-disable-line react-hooks/exhaustive-deps

  if (benches.loading || models.loading) return <Loading />;
  if (benches.error) return <ErrorNotice error={benches.error} retry={benches.reload} />;
  if (!benches.data?.length) {
    return (
      <div className="page">
        <PageHead title="Chạy benchmark mới" />
        <div className="card empty">
          <h2>Chưa có bộ bench nào</h2>
          <p>Kết nối repo bench trước, hệ thống sẽ đọc manifest để lấy danh sách bộ bench.</p>
          <Link className="btn btn-primary" to="/sources">Kết nối repo bench</Link>
        </div>
      </div>
    );
  }

  const fb = benches.data.find((b) => b.id === form.bench_id) || benches.data[0];
  const isHarbor = fb.scoring_type === "harbor";
  // Bộ bench Harbor chỉ chạy được với endpoint OpenAI-compatible hoặc agent Harbor; agent Harbor không chạy bộ hỏi–đáp.
  const usable = (kind: string) => (isHarbor ? kind !== "mock" : kind !== "harbor");
  const n = form.samples === "all" ? fb.size : Math.min(Number(form.samples), fb.size);
  const calls = n * form.models.length;
  const judgeWarn = fb.judge_required && form.judge_id != null && form.models.includes(form.judge_id);
  const running = !!active.data;
  const missingJudge = fb.judge_required && !form.judge_id;
  const unusable = form.models.filter((id) => !usable(contestants.find((m) => m.id === id)?.kind || ""));
  const disabled = !form.models.length || running || missingJudge || submitting || unusable.length > 0;

  async function submit() {
    setSubmitting(true);
    setError(undefined);
    try {
      const run = await api.createRun({
        ...form,
        bench_id: fb.id,
        judge_id: fb.judge_required ? form.judge_id : null,
        samples: form.samples === "all" ? "all" : Number(form.samples),
      });
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError(errMsg(e));
      setSubmitting(false);
    }
  }

  const toggleModel = (id: string) =>
    set({ models: form.models.includes(id) ? form.models.filter((x) => x !== id) : [...form.models, id] });

  return (
    <div className="page">
      <PageHead title="Chạy benchmark mới" lead="Chọn bộ bench, thí sinh và giám khảo rồi bấm Chạy. Log sẽ hiện trực tiếp ngay sau đó." />
      <div className="split">
        <div className="grow">
          <section aria-labelledby="s1" className="card stack">
            <Step n={1} id="s1" title="Bộ bench" aside={<Link className="link" to="/sources" style={{ fontSize: 13 }}>Quản lý nguồn bench</Link>} />
            <div role="group" aria-label="Chọn bộ bench" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 12 }}>
              {benches.data.map((b) => (
                <button key={b.id} type="button" className="option-card" aria-pressed={fb.id === b.id}
                  onClick={() => set(b.scoring_type === "harbor"
                    // Task Harbor nặng (Docker, 4–32 GB RAM, có thể chạy hàng giờ): mặc định ít luồng.
                    ? { bench_id: b.id, concurrency: Math.min(form.concurrency, 2), samples: "all", models: form.models.filter((id) => contestants.find((m) => m.id === id)?.kind !== "mock") }
                    : { bench_id: b.id, models: form.models.filter((id) => contestants.find((m) => m.id === id)?.kind !== "harbor") })}>
                  <span style={{ fontWeight: 600 }}>{b.name}</span>
                  <span className="muted" style={{ fontSize: 13 }}>{b.task}</span>
                  <span className="chips" style={{ gap: 6, marginTop: 6 }}>
                    <span className="tag">{b.scoring}</span>
                    <span className="tag">{b.size} {b.scoring_type === "harbor" ? "task" : "mẫu"}</span>
                  </span>
                  <span className="mono muted" style={{ marginTop: 6, fontSize: 11.5, overflowWrap: "anywhere" }}>{b.source}</span>
                </button>
              ))}
            </div>
          </section>

          <section aria-labelledby="s2" className="card stack">
            <Step n={2} id="s2" title="Thí sinh" aside={<span className="muted" style={{ fontSize: 13 }}>Chọn một hoặc nhiều mô hình, tất cả làm cùng một tập mẫu</span>} />
            {contestants.length === 0 && <p className="notice neutral">Chưa có thí sinh. <Link to="/models">Thêm mô hình</Link>.</p>}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 10 }}>
              {contestants.map((m) => {
                const on = form.models.includes(m.id);
                const wrongKind = !usable(m.kind);
                const down = (m.status === "down" || wrongKind) && !on;
                const note = wrongKind
                  ? (isHarbor ? "Mock không chạy được task Harbor" : "Agent Harbor chỉ chạy bộ bench Harbor")
                  : m.status === "down" ? "Không phản hồi · xem Mô hình"
                  : isHarbor && m.kind === "openai" ? `agent ${m.agent || "terminus-2"} · ${m.note}`
                  : m.note;
                return (
                  <label key={m.id} className={`check-row ${on ? "on" : ""} ${down ? "off" : ""}`}>
                    <input type="checkbox" checked={on} disabled={down} onChange={() => toggleModel(m.id)} />
                    <span className="mono" style={{ fontWeight: 500, fontSize: 13 }}>{m.id}</span>
                    <span style={{ marginLeft: "auto", fontSize: 12, textAlign: "right", color: down || (on && wrongKind) ? "var(--err)" : "var(--muted)" }}>
                      {note}
                    </span>
                  </label>
                );
              })}
            </div>
          </section>

          <section aria-labelledby="s3" className="card stack">
            <Step n={3} id="s3" title="Giám khảo" />
            {fb.judge_required ? (
              <>
                <div className="fields" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
                  <label className="field">Mô hình giám khảo
                    <select className="input" value={form.judge_id ?? ""} onChange={(e) => set({ judge_id: e.target.value || null })}>
                      {judges.length === 0 && <option value="">Chưa có giám khảo phù hợp</option>}
                      {judges.map((j) => <option key={j.id} value={j.id}>{j.id}{j.status === "down" ? " (không phản hồi)" : ""}</option>)}
                    </select>
                  </label>
                  <label className="field">Rubric chấm (từ manifest)
                    <input className="input mono" readOnly value={fb.rubric_path || "Rubric mặc định"} />
                  </label>
                  <label className="field">Ngưỡng đạt
                    <select className="input" value={form.threshold} onChange={(e) => set({ threshold: Number(e.target.value) })}>
                      {[5, 6, 7, 8].map((t) => <option key={t} value={t}>≥ {t} / 10</option>)}
                    </select>
                  </label>
                </div>
                {judgeWarn && <p role="status" className="notice warn">Giám khảo {form.judge_id} cũng đang là thí sinh — điểm có thể bị thiên vị. Nên chọn giám khảo khác.</p>}
              </>
            ) : isHarbor ? (
              <p className="notice neutral">
                {fb.name} chạy bằng Harbor: mỗi task dựng môi trường Docker riêng, agent làm bài trong terminal, rồi verifier của task chấm (reward 0–1). Không cần giám khảo.
                {fb.prepare.length > 0 && <> Trước khi chạy, hệ thống sẽ chạy bước chuẩn bị: <span className="mono">{fb.prepare.join(" && ")}</span>.</>}
              </p>
            ) : (
              <p className="notice neutral">{fb.name} được chấm tự động ({fb.scoring}), không cần giám khảo.</p>
            )}
          </section>

          <section aria-labelledby="s4" className="card stack">
            <Step n={4} id="s4" title="Tham số chạy" />
            <div className="fields" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
              <label className="field">Temperature
                <input className="input" type="number" min={0} max={2} step={0.1} value={form.temperature} onChange={(e) => set({ temperature: Number(e.target.value) })} />
              </label>
              <label className="field">Max tokens
                <input className="input" type="number" min={64} step={64} value={form.max_tokens} onChange={(e) => set({ max_tokens: Number(e.target.value) })} />
              </label>
              <label className="field">{isHarbor ? "Số task" : "Số mẫu"}
                <select className="input" value={form.samples} onChange={(e) => set({ samples: e.target.value })}>
                  {isHarbor && <option value="1">1 (thử một task)</option>}
                  {isHarbor && <option value="5">5</option>}
                  <option value="20">20{isHarbor ? "" : " (chạy thử)"}</option>
                  <option value="40">40</option>
                  <option value="all">Toàn bộ ({fb.size})</option>
                </select>
              </label>
              <label className="field">{isHarbor ? "Task song song" : "Luồng song song"}
                <select className="input" value={form.concurrency} onChange={(e) => set({ concurrency: Number(e.target.value) })}>
                  {[1, 2, 4, 8, 16].map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label className="field">Seed
                <input className="input" type="text" inputMode="numeric" value={form.seed} onChange={(e) => set({ seed: Number(e.target.value.replace(/\D/g, "")) || 0 })} />
              </label>
            </div>
          </section>
        </div>

        <aside aria-labelledby="sum-h" className="side card stack" style={{ gap: 16 }}>
          <h2 id="sum-h" className="card-title">Tóm tắt lượt chạy</h2>
          <dl className="summary">
            <div><dt>Bộ bench</dt><dd>{fb.name}</dd></div>
            <div><dt>Nguồn</dt><dd className="mono" style={{ fontWeight: 500 }}>{fb.source}</dd></div>
            <div><dt>Thí sinh ({form.models.length})</dt><dd>{form.models.length ? form.models.join(", ") : "Chưa chọn"}</dd></div>
            <div><dt>Giám khảo</dt><dd>{fb.judge_required ? `${form.judge_id ?? "Chưa chọn"} · ${fb.rubric_path || "rubric mặc định"}` : isHarbor ? "Verifier của task" : "Chấm tự động"}</dd></div>
            <div><dt>{isHarbor ? "Số task mỗi thí sinh" : "Số mẫu mỗi thí sinh"}</dt><dd>{n} / {fb.size}</dd></div>
            {isHarbor ? (
              <div><dt>Lượt chạy Harbor</dt><dd>{fmtInt(calls)} (mỗi lượt một container)</dd></div>
            ) : (
              <>
                <div><dt>Lượt gọi thí sinh</dt><dd>{fmtInt(calls)}</dd></div>
                <div><dt>Lượt gọi giám khảo</dt><dd>{fb.judge_required ? fmtInt(calls) : "0"}</dd></div>
              </>
            )}
          </dl>
          {error && <p role="alert" className="notice err">{error}</p>}
          <button type="button" className="btn btn-primary btn-lg" disabled={disabled} onClick={submit}>
            <Icon d={ICONS.play} size={16} color="#fff" />
            {running ? "Đang có lượt chạy…" : submitting ? "Đang tạo…" : `Chạy ${form.models.length} thí sinh`}
          </button>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {running ? (
              <>Chờ lượt chạy hiện tại xong hoặc dừng nó ở <Link to={`/runs/${active.data!.id}`}>Đang chạy</Link>.</>
            ) : !form.models.length ? "Chọn ít nhất một thí sinh để chạy."
              : unusable.length ? `Bỏ chọn ${unusable.join(", ")}: không chạy được bộ bench này.`
              : missingJudge ? "Bộ bench này cần một giám khảo."
              : isHarbor ? "Cần Docker đang chạy trên máy chủ. Mỗi task có thể mất từ vài phút tới hàng giờ."
              : "Sau khi bấm, bạn sẽ được chuyển tới màn hình log trực tiếp."}
          </p>
        </aside>
      </div>
    </div>
  );
}
