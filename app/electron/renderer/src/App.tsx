import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  AppSettings,
  InspectResult,
  MeshObjectSummary,
  Project,
  RunView,
} from '@shared/contracts';
import { ReportTabs } from './ReportTabs';

type Banner = { kind: 'error' | 'info'; text: string } | null;

const TERMINAL = new Set(['accepted', 'rejected', 'failed', 'cancelled']);

export function App(): JSX.Element {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [inspect, setInspect] = useState<InspectResult | null>(null);
  const [selectedObject, setSelectedObject] = useState<string>('');
  const [targetFaces, setTargetFaces] = useState<number>(12000);
  const [preserveFeatures, setPreserveFeatures] = useState<boolean>(true);
  const [proxyTargetFaces, setProxyTargetFaces] = useState<number>(1_000_000);
  const [retryLadderAttempts, setRetryLadderAttempts] = useState<number>(1);
  const [runId, setRunId] = useState<string | null>(null);
  const [runView, setRunView] = useState<RunView | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [banner, setBanner] = useState<Banner>(null);

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    window.api.settingsGet().then(setSettings);
  }, []);

  const refreshRun = useCallback(async () => {
    if (!project || !runId) return;
    const view = await window.api.runGet({ projectId: project.id, runId });
    setRunView(view);
    if (view.status && TERMINAL.has(view.status.status) && pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, [project, runId]);

  // Poll the active run; also refresh on main-process push events.
  useEffect(() => {
    if (!project || !runId) return;
    refreshRun();
    pollTimer.current = setInterval(refreshRun, 1000);
    const off = window.api.onRunUpdate((p) => {
      if (p.projectId === project.id && p.runId === runId) refreshRun();
    });
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = null;
      off();
    };
  }, [project, runId, refreshRun]);

  const guard = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setBanner(null);
    try {
      await fn();
    } catch (err) {
      setBanner({ kind: 'error', text: String((err as Error)?.message ?? err) });
    } finally {
      setBusy(null);
    }
  };

  const onImport = () =>
    guard('Importing', async () => {
      const sourcePath = await window.api.pickFile();
      if (!sourcePath) return;
      const name = sourcePath.split(/[\\/]/).pop()?.replace(/\.[^.]+$/, '') ?? 'project';
      const p = await window.api.projectCreate({ name, sourcePath });
      setProject(p);
      setInspect(null);
      setRunId(null);
      setRunView(null);
      setBanner({ kind: 'info', text: `Project created at ${p.dir}` });
    });

  const onOpenProject = () =>
    guard('Opening', async () => {
      const dir = await window.api.pickProjectDir();
      if (!dir) return;
      const p = await window.api.projectOpen(dir);
      setProject(p);
      setInspect(null);
      setRunId(p.approved_lowpoly_run_id ?? (p.runs.length ? p.runs[p.runs.length - 1] : null));
      setRunView(null);
    });

  const onInspect = () =>
    guard('Inspecting', async () => {
      if (!project) return;
      const res = await window.api.modelInspect({ projectId: project.id });
      setInspect(res);
      if (res.status === 'accepted' && res.objects?.length) {
        const first = res.objects[0];
        setSelectedObject(first.name);
      } else if (res.status === 'failed') {
        setBanner({ kind: 'error', text: res.error?.message ?? 'inspect failed' });
      }
    });

  const onGenerate = () =>
    guard('Generating', async () => {
      if (!project || !selectedObject) {
        setBanner({ kind: 'error', text: 'Select an object first' });
        return;
      }
      const { run_id } = await window.api.lowpolyGenerate({
        projectId: project.id,
        objectName: selectedObject,
        targetFaces,
        options: {
          preserve_features: preserveFeatures,
          render_preview: true,
          proxy_target_faces: proxyTargetFaces,
          retry_ladder_max_attempts: retryLadderAttempts,
        },
      });
      setRunId(run_id);
      setRunView(null);
    });

  const onApprove = () =>
    guard('Approving', async () => {
      if (!project || !runId) return;
      const res = await window.api.lowpolyApprove({ projectId: project.id, runId });
      const p = await window.api.projectGet(project.id);
      setProject(p);
      setBanner({ kind: 'info', text: `Approved. working_model = ${res.working_model}` });
    });

  const status = runView?.status?.status ?? null;
  const isApproved = !!project?.approved_lowpoly_run_id;
  const canApprove = status === 'accepted' && !!runView?.summary?.artifacts?.lowpoly_blend;

  return (
    <div className="shell">
      <TopBar
        busy={busy}
        hasProject={!!project}
        hasInspect={!!inspect?.objects?.length}
        canGenerate={!!selectedObject}
        canApprove={canApprove}
        onImport={onImport}
        onOpen={onOpenProject}
        onInspect={onInspect}
        onGenerate={onGenerate}
        onApprove={onApprove}
      />

      {settings && <SettingsBar settings={settings} onChange={setSettings} />}
      {banner && <div className={`banner ${banner.kind}`}>{banner.text}</div>}

      <div className="body">
        <LeftPanel
          project={project}
          inspect={inspect}
          selectedObject={selectedObject}
          onSelectObject={setSelectedObject}
          activeRunId={runId}
          onSelectRun={setRunId}
        />

        <main className="center">
          <PreviewPane runView={runView} />
        </main>

        <RightPanel
          objects={inspect?.objects ?? []}
          selectedObject={selectedObject}
          targetFaces={targetFaces}
          onTargetFaces={setTargetFaces}
          preserveFeatures={preserveFeatures}
          onPreserveFeatures={setPreserveFeatures}
          proxyTargetFaces={proxyTargetFaces}
          onProxyTargetFaces={setProxyTargetFaces}
          retryLadderAttempts={retryLadderAttempts}
          onRetryLadderAttempts={setRetryLadderAttempts}
          runView={runView}
          isApproved={isApproved}
          approvedRunId={project?.approved_lowpoly_run_id ?? null}
        />
      </div>

      <BottomPanel runView={runView} status={status} />
    </div>
  );
}

function TopBar(props: {
  busy: string | null;
  hasProject: boolean;
  hasInspect: boolean;
  canGenerate: boolean;
  canApprove: boolean;
  onImport: () => void;
  onOpen: () => void;
  onInspect: () => void;
  onGenerate: () => void;
  onApprove: () => void;
}): JSX.Element {
  return (
    <header className="topbar">
      <span className="brand">UV Review · MVP 0</span>
      <button onClick={props.onImport}>Import</button>
      <button onClick={props.onOpen}>Open</button>
      <button disabled={!props.hasProject} onClick={props.onInspect}>
        Inspect
      </button>
      <button disabled={!props.canGenerate} onClick={props.onGenerate}>
        Generate Low-poly
      </button>
      <button disabled={!props.canApprove} className="primary" onClick={props.onApprove}>
        Approve
      </button>
      <span className="busy">{props.busy ? `${props.busy}…` : ''}</span>
    </header>
  );
}

function SettingsBar(props: {
  settings: AppSettings;
  onChange: (s: AppSettings) => void;
}): JSX.Element {
  const { settings } = props;
  const setBlender = async () => {
    const path = window.prompt('Blender executable path', settings.blenderPath ?? '');
    if (path === null) return;
    const next = await window.api.settingsSet({ blenderPath: path || null });
    props.onChange(next);
  };
  return (
    <div className={`settingsbar ${settings.blenderPath ? '' : 'warn'}`}>
      <span>
        Blender:&nbsp;
        {settings.blenderPath ? <code>{settings.blenderPath}</code> : <strong>not configured — generation will use mock</strong>}
      </span>
      <button onClick={setBlender}>Set Blender path</button>
    </div>
  );
}

function LeftPanel(props: {
  project: Project | null;
  inspect: InspectResult | null;
  selectedObject: string;
  onSelectObject: (n: string) => void;
  activeRunId: string | null;
  onSelectRun: (id: string) => void;
}): JSX.Element {
  const { project, inspect } = props;
  return (
    <aside className="left">
      <section>
        <h3>Project</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="muted small">{project.dir}</div>
            <div className="small">
              source: <code>{project.source_model}</code>
            </div>
            {project.working_model && (
              <div className="small ok">working: {project.working_model}</div>
            )}
          </div>
        ) : (
          <div className="muted">No project. Import a model to begin.</div>
        )}
      </section>

      <section>
        <h3>Objects</h3>
        {inspect?.objects?.length ? (
          <ul className="list">
            {inspect.objects.map((o: MeshObjectSummary) => (
              <li
                key={o.name}
                className={o.name === props.selectedObject ? 'sel' : ''}
                onClick={() => props.onSelectObject(o.name)}
              >
                {o.name} <span className={`tag ${o.mesh_role_hint}`}>{o.mesh_role_hint}</span>
                <div className="muted small">{o.faces.toLocaleString()} faces</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">Run Inspect to list objects.</div>
        )}
      </section>

      <section>
        <h3>Runs</h3>
        {project?.runs?.length ? (
          <ul className="list">
            {project.runs.map((r) => (
              <li
                key={r}
                className={r === props.activeRunId ? 'sel' : ''}
                onClick={() => props.onSelectRun(r)}
              >
                <code className="small">{r.replace('run_', '').slice(0, 8)}</code>
                {r === project.approved_lowpoly_run_id && <span className="tag ok">approved</span>}
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">No runs yet.</div>
        )}
      </section>
    </aside>
  );
}

function PreviewPane(props: { runView: RunView | null }): JSX.Element {
  const preview = props.runView?.preview_path;
  return (
    <div className="preview">
      {preview ? (
        <img alt="low-poly preview" src={`uvpreview://${preview}`} />
      ) : (
        <div className="placeholder">
          Preview appears here after generation.
          <div className="muted small">(MVP 0 uses rendered image preview)</div>
        </div>
      )}
    </div>
  );
}

function RightPanel(props: {
  objects: MeshObjectSummary[];
  selectedObject: string;
  targetFaces: number;
  onTargetFaces: (n: number) => void;
  preserveFeatures: boolean;
  onPreserveFeatures: (b: boolean) => void;
  proxyTargetFaces: number;
  onProxyTargetFaces: (n: number) => void;
  retryLadderAttempts: number;
  onRetryLadderAttempts: (n: number) => void;
  runView: RunView | null;
  isApproved: boolean;
  approvedRunId: string | null;
}): JSX.Element {
  const obj = props.objects.find((o) => o.name === props.selectedObject) ?? null;
  const m = props.runView?.summary?.metrics ?? null;
  const warnings = props.runView?.summary?.warnings ?? [];
  return (
    <aside className="right">
      <section>
        <h3>Target setup</h3>
        <label className="field">
          Target face count
          <input
            type="number"
            min={100}
            step={500}
            value={props.targetFaces}
            onChange={(e) => props.onTargetFaces(Number(e.target.value))}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={props.preserveFeatures}
            onChange={(e) => props.onPreserveFeatures(e.target.checked)}
          />
          Preserve features
        </label>
        <label className="field">
          Voxel proxy target faces
          <input
            type="number"
            min={50000}
            step={100000}
            value={props.proxyTargetFaces}
            onChange={(e) => props.onProxyTargetFaces(Number(e.target.value))}
          />
          <span className="muted small">
            대형 소스(&gt; 1.5M faces)만: decimate 전 voxel remesh 프록시 밀도
          </span>
        </label>
        <label className="field">
          Retry ladder attempts
          <input
            type="number"
            min={0}
            max={6}
            step={1}
            value={props.retryLadderAttempts}
            onChange={(e) => props.onRetryLadderAttempts(Number(e.target.value))}
          />
          <span className="muted small">
            목표 미달 시 escalation 횟수. 1 = 단일 시도(빠름), 0 = 끔, 높을수록 정밀·느림
          </span>
        </label>
      </section>

      <section>
        <h3>Mesh summary</h3>
        {obj ? (
          <table className="metrics">
            <tbody>
              <tr><td>name</td><td>{obj.name}</td></tr>
              <tr><td>role</td><td>{obj.mesh_role_hint}</td></tr>
              <tr><td>vertices</td><td>{obj.vertices.toLocaleString()}</td></tr>
              <tr><td>edges</td><td>{obj.edges.toLocaleString()}</td></tr>
              <tr><td>faces</td><td>{obj.faces.toLocaleString()}</td></tr>
              <tr><td>uv layers</td><td>{obj.uv_layers.join(', ') || '—'}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">Select an object.</div>
        )}
      </section>

      <section>
        <h3>Result metrics</h3>
        {m ? (
          <table className="metrics">
            <tbody>
              <tr><td>target faces</td><td>{m.target_faces ?? '—'}</td></tr>
              <tr className="hl"><td>actual faces</td><td>{m.actual_faces ?? '—'}</td></tr>
              <tr><td>source faces</td><td>{m.source_faces ?? '—'}</td></tr>
              <tr><td>non-manifold</td><td>{m.non_manifold_edges ?? '—'}</td></tr>
              <tr><td>surf dist mean</td><td>{fmt(m.surface_distance_mean_ratio)}</td></tr>
              <tr><td>normal dev°</td><td>{fmt(m.normal_deviation_mean_deg)}</td></tr>
            </tbody>
          </table>
        ) : (
          <div className="muted">Run generation to see metrics.</div>
        )}
      </section>

      {warnings.length > 0 && (
        <section>
          <h3>Warnings</h3>
          <ul className="warnlist">
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </section>
      )}

      {props.isApproved && (
        <section>
          <div className="ok">✓ Approved working low-poly</div>
          <div className="muted small">{props.approvedRunId}</div>
        </section>
      )}
    </aside>
  );
}

function BottomPanel(props: { runView: RunView | null; status: string | null }): JSX.Element {
  return (
    <footer className="bottom">
      <div className="statusrow">
        <span className={`statuspill ${props.status ?? 'idle'}`}>
          {props.status ?? 'idle'}
        </span>
        {props.runView?.status?.error && (
          <span className="err">
            {props.runView.status.error.code}: {props.runView.status.error.message}
          </span>
        )}
      </div>
      <ReportTabs runView={props.runView} />
    </footer>
  );
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(4);
}
