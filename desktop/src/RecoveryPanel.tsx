import { useCallback, useEffect, useId, useRef, useState } from "react";
import { daemonFetch } from "./daemonClient";
import "./RecoveryPanel.css";

type Resolution = "confirmed_applied" | "confirmed_not_applied" | "reviewed";
type Recovery = {
  run_id: string;
  invocation_id: string;
  tool_name: string;
  effect: string | null;
  detected_at: string;
  resolution: Resolution | null;
  acknowledged_at: string | null;
};
type RunDetails = {
  run: { run_id: string; status: string; started_at: string; updated_at: string };
  effects: Array<{ invocation_id: string; status: string; started_at: string | null; completed_at: string | null }>;
};

const resolutions: Record<Resolution, string> = {
  confirmed_applied: "Confirmed applied",
  confirmed_not_applied: "Confirmed not applied",
  reviewed: "Reviewed — outcome remains uncertain",
};
const dateLabel = (value: string | null) => {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not recorded" : date.toLocaleString();
};
const statusLabel = (value: string) => ({ running: "Running", completed: "Completed", failed: "Interrupted or failed", cancelled: "Stopped", started: "Started", requested: "Requested", denied: "Denied", approval_required: "Awaiting approval" }[value] || "Unknown");
const effectLabel = (value: string | null) => ({ read_only: "Read only", workspace_write: "Workspace change", code_execution: "Code execution", external_side_effect: "External action", orchestration: "Task coordination" }[value || ""] || "Tool operation");
const isRecovery = (value: unknown): value is Recovery => {
  if (!value || typeof value !== "object") return false;
  const record = value as Recovery;
  return [record.run_id, record.invocation_id, record.tool_name, record.detected_at].every((field) => typeof field === "string" && field.length > 0)
    && (record.effect === null || typeof record.effect === "string")
    && (record.resolution === null || Object.prototype.hasOwnProperty.call(resolutions, record.resolution))
    && (record.acknowledged_at === null || typeof record.acknowledged_at === "string");
};
const requestError = (status: number) => status === 401 || status === 403
  ? "Access to the local service was denied. Reopen Jarvs and try again."
  : "The local service could not complete this request. Try again.";

function RecoveryItem({ record, onConfirmed, onStale }: {
  record: Recovery;
  onConfirmed: (record: Recovery) => void;
  onStale: () => void;
}) {
  const choiceId = useId();
  const [choice, setChoice] = useState<Resolution | "">("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const pending = useRef(false);
  const requests = useRef(new Set<AbortController>());
  useEffect(() => () => { requests.current.forEach((controller) => controller.abort()); }, []);

  const inspect = async () => {
    setDetailsOpen(true);
    if (details || detailsLoading) return;
    const controller = new AbortController();
    requests.current.add(controller);
    setDetailsLoading(true);
    setDetailsError(null);
    try {
      const response = await daemonFetch(`/runs/${encodeURIComponent(record.run_id)}`, { signal: controller.signal });
      if (!response.ok) throw new Error(response.status === 404 ? "This run is no longer available." : requestError(response.status));
      const data = await response.json();
      if (data.run?.run_id !== record.run_id || !Array.isArray(data.effects)) throw new Error("Run details could not be read. Try again.");
      if (!controller.signal.aborted) setDetails(data);
    } catch (failure) {
      if (!controller.signal.aborted) setDetailsError(failure instanceof Error ? failure.message : "Run details are unavailable.");
    } finally {
      requests.current.delete(controller);
      if (!controller.signal.aborted) setDetailsLoading(false);
    }
  };

  const acknowledge = async () => {
    if (!choice || pending.current || record.resolution) return;
    pending.current = true;
    const controller = new AbortController();
    requests.current.add(controller);
    setSaving(true);
    setError(null);
    try {
      const response = await daemonFetch(`/run-recovery/${encodeURIComponent(record.run_id)}/${encodeURIComponent(record.invocation_id)}/acknowledge`, {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ resolution: choice }),
      });
      if (controller.signal.aborted) return;
      if (response.status === 404 || response.status === 409) {
        onStale();
        return;
      }
      if (!response.ok) throw new Error(requestError(response.status));
      const data = await response.json();
      if (!isRecovery(data.recovery) || data.recovery.run_id !== record.run_id || data.recovery.invocation_id !== record.invocation_id || data.recovery.resolution !== choice || !data.recovery.acknowledged_at) {
        throw new Error("The confirmation could not be verified. Refresh the list before trying again.");
      }
      if (!controller.signal.aborted) onConfirmed(data.recovery);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "The confirmation could not be saved. Refresh to check its status.");
    } finally {
      requests.current.delete(controller);
      pending.current = false;
      if (!controller.signal.aborted) setSaving(false);
    }
  };
  const effect = details?.effects.find((item) => item.invocation_id === record.invocation_id);

  return (
    <li className="recovery-item">
      <h4>{record.tool_name}</h4>
      <p className="recovery-muted">{effectLabel(record.effect)} · Detected {dateLabel(record.detected_at)}</p>
      {record.resolution ? (
        <p className="recovery-confirmed" role="status">{resolutions[record.resolution]} · {dateLabel(record.acknowledged_at)}</p>
      ) : <p className="recovery-uncertain">Outcome uncertain — check the affected files or service before confirming.</p>}
      <button type="button" className="recovery-text-button" aria-expanded={detailsOpen} onClick={() => detailsOpen ? setDetailsOpen(false) : void inspect()}>
        {detailsOpen ? "Hide run details" : "Inspect run"}
      </button>
      {detailsOpen && (
        <div className="recovery-details">
          {detailsLoading && <p role="status">Loading run details…</p>}
          {detailsError && <p role="alert">{detailsError} <button type="button" className="recovery-text-button" onClick={() => void inspect()}>Retry details</button></p>}
          {details && <dl>
            <dt>Run</dt><dd>{details.run.run_id}</dd>
            <dt>Operation</dt><dd>{record.invocation_id}</dd>
            <dt>Run status</dt><dd>{statusLabel(details.run.status)}</dd>
            <dt>Run started</dt><dd>{dateLabel(details.run.started_at)}</dd>
            <dt>Last update</dt><dd>{dateLabel(details.run.updated_at)}</dd>
            {effect && <><dt>Recorded tool status</dt><dd>{statusLabel(effect.status)}</dd><dt>Tool started</dt><dd>{dateLabel(effect.started_at)}</dd><dt>Tool finished</dt><dd>{dateLabel(effect.completed_at)}</dd></>}
          </dl>}
          <p className="recovery-muted">Recorded status does not establish whether the effect occurred.</p>
        </div>
      )}
      {!record.resolution && <div className="recovery-actions">
        <label htmlFor={choiceId}>Your verification</label>
        <div className="recovery-action-row">
          <select id={choiceId} value={choice} disabled={saving} onChange={(event) => setChoice(event.target.value as Resolution | "")}>
            <option value="">Choose an outcome…</option>
            {Object.entries(resolutions).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <button type="button" className="recovery-confirm-button" disabled={!choice || saving} onClick={() => void acknowledge()}>{saving ? "Saving…" : "Confirm"}</button>
        </div>
        {error && <p className="recovery-error" role="alert">{error}</p>}
      </div>}
    </li>
  );
}

export default function RecoveryPanel() {
  const [records, setRecords] = useState<Recovery[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const response = await daemonFetch("/run-recovery", { signal: controller.signal });
      if (!response.ok) throw new Error(requestError(response.status));
      const data = await response.json();
      if (!Array.isArray(data.recoveries) || !data.recoveries.every(isRecovery)) throw new Error("Recovery records could not be read. Try again.");
      if (!controller.signal.aborted) setRecords(data.recoveries);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Recovery records are unavailable. Try again.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load]);

  return (
    <section className="recovery-panel" aria-label="Interrupted operations">
      <div className="recovery-heading"><h3>Operations to verify</h3><button type="button" className="recovery-text-button" disabled={loading} onClick={() => { setNotice(null); void load(); }}>Refresh</button></div>
      <p className="recovery-muted">After an interruption, some operations may have taken effect without a recorded result. Review them here; confirmation never runs them again.</p>
      {notice && <p role="status" className="recovery-notice">{notice}</p>}
      {loading && <p role="status">Loading operations…</p>}
      {error && <p role="alert" className="recovery-error">{error} <button type="button" className="recovery-text-button" onClick={() => void load()}>Retry</button></p>}
      {!loading && !error && records.length === 0 && <p className="recovery-empty">No operations need verification.</p>}
      {!loading && !error && records.length >= 100 && <p className="recovery-muted">Showing up to 100 operations. Confirm reviewed items and refresh to check for more.</p>}
      {!loading && !error && records.length > 0 && <ul className="recovery-list">{records.map((record) => (
        <RecoveryItem key={`${record.run_id}:${record.invocation_id}`} record={record}
          onConfirmed={(updated) => setRecords((current) => current.map((item) => item.run_id === updated.run_id && item.invocation_id === updated.invocation_id ? updated : item))}
          onStale={() => { setNotice("This operation changed or was already reviewed elsewhere. Check the latest list before continuing."); void load(); }} />
      ))}</ul>}
    </section>
  );
}
