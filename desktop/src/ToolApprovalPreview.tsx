import "./ToolApprovalPreview.css";

export type ApprovalArguments = Record<string, unknown> & { language?: string; code?: string };

const MAX_EDITS = 6;
const MAX_SNIPPET_CHARS = 1200;
const MAX_SNIPPET_LINES = 16;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function excerpt(value: unknown, maxChars = MAX_SNIPPET_CHARS, maxLines = MAX_SNIPPET_LINES) {
  if (typeof value !== "string") return { text: "(not provided)", truncated: false };
  if (!value) return { text: "(empty text)", truncated: false };
  const text = value.slice(0, maxChars).split("\n").slice(0, maxLines).join("\n");
  return { text, truncated: text.length < value.length };
}

function Snippet({ label, value }: { label: string; value: unknown }) {
  const preview = excerpt(value);
  return (
    <div className="edit-preview-snippet">
      <p className="edit-preview-label">{label}</p>
      <pre aria-label={label}><code>{preview.text}</code></pre>
      {preview.truncated && <p className="edit-preview-omitted">Text omitted after {MAX_SNIPPET_CHARS} characters or {MAX_SNIPPET_LINES} lines.</p>}
    </div>
  );
}

export default function ToolApprovalPreview({ toolName, arguments: args }: {
  toolName: string;
  arguments: ApprovalArguments;
}) {
  if (toolName !== "workspace.edit_text") {
    const code = args.code || (Object.keys(args).length ? JSON.stringify(args, null, 2) : "");
    return <pre className="whitespace-pre-wrap break-words rounded-lg border border-jarvs-border bg-black/20 p-3 text-[11px] leading-relaxed select-text"><code>{code || "(no details available)"}</code></pre>;
  }

  const edits = Array.isArray(args.edits) ? args.edits : [];
  const counts = edits.map(edit => {
    const count = record(edit).expected_count;
    return count === undefined ? 1 : typeof count === "number" && Number.isSafeInteger(count) && count > 0 ? count : null;
  });
  const total = counts.every(count => count !== null) ? counts.reduce<number>((sum, count) => sum + (count ?? 0), 0) : null;
  const path = excerpt(args.path, 1024, 4);
  const digest = typeof args.expected_sha256 === "string" && /^[a-f\d]{64}$/i.test(args.expected_sha256) ? args.expected_sha256 : null;
  const shown = edits.slice(0, MAX_EDITS);
  const limited = edits.length > MAX_EDITS || path.truncated || shown.some(edit => {
    const item = record(edit);
    return excerpt(item.old_text).truncated || excerpt(item.new_text).truncated;
  });

  return (
    <section className="edit-preview" aria-label="File edit preview">
      <dl className="edit-preview-metadata">
        <dt>File</dt><dd className="edit-preview-path">{path.text}{path.truncated && <span className="edit-preview-omitted"> (path truncated)</span>}</dd>
        <dt>Changes</dt><dd>{edits.length} {edits.length === 1 ? "edit" : "edits"}{total !== null ? ` · ${total} expected ${total === 1 ? "replacement" : "replacements"}` : " · expected count unavailable"}</dd>
        <dt>Version check</dt><dd>{digest ? <>File must still match this SHA-256:<code className="edit-preview-digest">{digest}</code></> : "No valid file version provided."}</dd>
      </dl>
      {limited && <p className="edit-preview-notice" role="note">Preview limited. Approval applies to the entire operation, including omitted content.</p>}
      {edits.length === 0 && <p>No replacement details provided.</p>}
      <ol className="edit-preview-list">
        {shown.map((edit, index) => {
          const item = record(edit);
          return <li key={index}>
            <h3>Edit {index + 1}</h3>
            <p className="edit-preview-count">Expected matches: {counts[index] ?? "unavailable"}{item.expected_count === undefined && " (default)"}</p>
            <Snippet label={`Before · edit ${index + 1}`} value={item.old_text} />
            <Snippet label={`After · edit ${index + 1}`} value={item.new_text} />
          </li>;
        })}
      </ol>
      {edits.length > shown.length && <p className="edit-preview-omitted">{edits.length - shown.length} additional edits omitted from this preview.</p>}
    </section>
  );
}
