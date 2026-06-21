/**
 * MVP 2 Seam Editor workspace (plan §8 Electron MVP 2 UX).
 *
 * Author user `seam` / `protected` edge states on the working low-poly mesh, with
 * the Blender-exported edge geometry as the only selectable-id source (plan §5).
 * The user's choices are the source of truth — nothing here unwraps/generates UVs
 * or auto-adds the mandatory-90 fold (plan §1, §13). Existing UV island boundaries
 * can be imported as a *draft* the user explicitly approves before saving (plan §1,
 * §6.4, §F). The saved `user_seam_spec.json` becomes the MVP 3 generate/optimize
 * input (plan §10, §16).
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  InspectUvResult,
  MeshSignature,
  Project,
  SeamEditorRunView,
  SeamSpec,
  SeamValidation,
  UvLayerInfo,
  UvObjectSummary,
} from '@shared/contracts';
import { SeamCommand, SEAM_TERMINAL_STATUSES, normalizeAndValidateSpec } from '@shared/contracts';
import type { Banner } from '../App';
import { useT, statusLabel } from '../i18n';
import { SeamViewport, type OverlayToggles } from './SeamViewport';
import {
  clearEdges,
  conflictEdges,
  invalidEdges,
  markProtected,
  markSeam,
  setsFromSpec,
  specFromSets,
  type SeamSets,
} from './seamEdits';

const EMPTY_SETS: SeamSets = { seams: new Set(), protectedEdges: new Set() };

export function SeamEditorWorkspace(props: {
  project: Project | null;
  setProject: (p: Project) => void;
  guard: (label: string, fn: () => Promise<void>) => Promise<void>;
  setBanner: (b: Banner) => void;
}): JSX.Element {
  const t = useT();
  const { project, guard, setBanner } = props;

  const [inspect, setInspect] = useState<InspectUvResult | null>(null);
  const [selectedObject, setSelectedObject] = useState<string>('');
  const [selectedLayer, setSelectedLayer] = useState<string>('');

  const [runId, setRunId] = useState<string | null>(null);
  const [runView, setRunView] = useState<SeamEditorRunView | null>(null);

  const [geometryRunId, setGeometryRunId] = useState<string | null>(null);
  const [runView2, setRunView2] = useState<SeamEditorRunView | null>(null);

  const [sets, setSets] = useState<SeamSets>(EMPTY_SETS);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [hovered, setHovered] = useState<number | null>(null);
  const [draft, setDraft] = useState<Set<number>>(new Set());
  const [boundaryReport, setBoundaryReport] = useState<SeamEditorRunView['boundary'] | null>(null);

  const [overlay, setOverlay] = useState<OverlayToggles>({
    showSeams: true,
    showProtected: true,
    showWire: true,
    showDraft: true,
  });
  const [tolerance, setTolerance] = useState(8);
  const [edgeIdInput, setEdgeIdInput] = useState('');
  const [savedValidation, setSavedValidation] = useState<SeamValidation | null>(null);

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // The geometry view (the export run) is kept independent of the latest action.
  const geometry = runView2?.edge_geometry ?? null;
  const meshSignature = runView2?.export_result?.mesh_signature ?? null;
  const edgeCount = meshSignature?.edges ?? geometry?.edges.length ?? null;

  // Reset per-project; seed object/layer from the manifest (plan §9).
  useEffect(() => {
    setInspect(null);
    setSelectedObject(project?.selected_object ?? '');
    setSelectedLayer(project?.selected_uv_layer ?? '');
    setRunId(null);
    setRunView(null);
    setGeometryRunId(null);
    setRunView2(null);
    setSets(EMPTY_SETS);
    setSelected(new Set());
    setDraft(new Set());
    setBoundaryReport(null);
    setSavedValidation(null);
  }, [project?.id]);

  // --- run polling (mirrors the UV review workspace) ---------------------
  const refreshRun = useCallback(async () => {
    if (!project || !runId) return;
    const view = await window.api.seamGetEditorRun({ projectId: project.id, runId });
    setRunView(view);
    if (runId === geometryRunId) setRunView2(view);
    if (view.status && SEAM_TERMINAL_STATUSES.has(view.status.status) && pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
      onRunComplete(view);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, runId, geometryRunId]);

  // Apply a completed run's artifacts (geometry export or boundary draft).
  const onRunComplete = (view: SeamEditorRunView) => {
    const cmd = view.status?.command;
    if (cmd === SeamCommand.ExportEdgeGeometry && view.edge_geometry) {
      setRunView2(view);
      setSelected(new Set());
      setBanner({
        kind: 'info',
        text: t('seam.geomLoaded', { n: view.edge_geometry.edges.length }),
      });
    } else if (cmd === SeamCommand.ExtractUvBoundary) {
      if (view.status?.status === 'no_uv') {
        setBanner({ kind: 'info', text: t('seam.noUvImport') });
        setDraft(new Set());
        setBoundaryReport(null);
      } else if (view.boundary?.spec) {
        setDraft(new Set(view.boundary.spec.user_seam_edges));
        setBoundaryReport(view.boundary);
        setBanner({
          kind: 'info',
          text: t('seam.draftLoaded', { n: view.boundary.user_seam_count ?? 0 }),
        });
      }
    } else if (view.status?.status === 'failed') {
      setBanner({ kind: 'error', text: view.status.error?.message ?? t('seam.workerFailed') });
    }
  };

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

  const objects = inspect?.objects ?? [];
  const currentObject = objects.find((o) => o.name === selectedObject) ?? null;
  const layers = currentObject?.uv_layers ?? [];

  // --- live validation (pure, no IPC) for counts + issues ----------------
  const validation = useMemo(
    () =>
      normalizeAndValidateSpec(specFromSets(selectedObject || 'object', sets), {
        edgeCount,
        objectName: selectedObject || null,
      }),
    [sets, edgeCount, selectedObject],
  );
  const conflict = useMemo(() => conflictEdges(sets), [sets]);
  const invalid = useMemo(() => invalidEdges(sets, edgeCount), [sets, edgeCount]);

  // --- actions ------------------------------------------------------------
  const onInspect = () =>
    guard(t('busy.inspectingMesh'), async () => {
      if (!project) return setBanner({ kind: 'error', text: t('common.openImportFirst') });
      const res = await window.api.uvInspectLayers({ projectId: project.id });
      setInspect(res);
      if (res.status === 'failed') {
        return setBanner({ kind: 'error', text: res.error?.message ?? t('common.inspectFailed') });
      }
      const first = res.objects?.find((o) => o.name === selectedObject) ?? res.objects?.[0];
      if (first) {
        setSelectedObject(first.name);
        setSelectedLayer(first.active_uv_layer ?? first.uv_layers[0]?.name ?? '');
      }
    });

  const onLoadMesh = () =>
    guard(t('busy.exportingEdge'), async () => {
      if (!project || !selectedObject) {
        return setBanner({ kind: 'error', text: t('seam.selectObjInspect') });
      }
      const { run_id } = await window.api.seamExportEdgeGeometry({
        projectId: project.id,
        objectName: selectedObject,
      });
      setGeometryRunId(run_id);
      setRunId(run_id);
      setRunView2(null);
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
    });

  const onExtractBoundary = () =>
    guard(t('busy.extractingBoundary'), async () => {
      if (!project || !selectedObject) {
        return setBanner({ kind: 'error', text: t('seam.selectObjFirst') });
      }
      const { run_id } = await window.api.seamExtractUvBoundary({
        projectId: project.id,
        objectName: selectedObject,
        uvLayer: selectedLayer || undefined,
      });
      setRunId(run_id);
    });

  const onSaveSpec = () =>
    guard(t('busy.savingSpec'), async () => {
      if (!project || !selectedObject) {
        return setBanner({ kind: 'error', text: t('seam.selectObjFirst') });
      }
      const spec = specFromSets(selectedObject, sets, { notes: 'Authored in Electron MVP2' });
      const res = await window.api.seamSaveSpec({
        projectId: project.id,
        spec,
        objectName: selectedObject,
        edgeCount,
      });
      setSavedValidation(res.validation);
      // Reflect the normalized (seam-wins, invalid-dropped) result in the editor.
      setSets(setsFromSpec(res.validation.normalized_spec));
      const p = await window.api.projectGet(project.id);
      props.setProject(p);
      setBanner({ kind: 'info', text: t('seam.saved', { path: res.path }) });
    });

  const onLoadSpec = () =>
    guard(t('busy.loadingSpec'), async () => {
      if (!project || !selectedObject) {
        return setBanner({ kind: 'error', text: t('seam.selectObjFirst') });
      }
      const res = await window.api.seamLoadSpec({
        projectId: project.id,
        objectName: selectedObject,
        edgeCount,
      });
      if (!res.spec) {
        return setBanner({ kind: 'info', text: t('seam.noSavedSpec') });
      }
      setSets(setsFromSpec(res.spec));
      setSavedValidation(res.validation);
      if (res.validation?.object_mismatch) {
        setBanner({
          kind: 'error',
          text: t('seam.objMismatchBanner', { a: res.spec.object, b: selectedObject }),
        });
      } else {
        setBanner({ kind: 'info', text: t('seam.loadedSpec', { n: res.spec.user_seam_edges.length }) });
      }
    });

  // --- selection + edit ---------------------------------------------------
  const pick = (id: number | null, additive: boolean) => {
    setSelected((prev) => {
      if (id === null) return additive ? prev : new Set();
      const next = new Set(additive ? prev : []);
      if (additive && next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const apply = (fn: (s: SeamSets, sel: Iterable<number>) => SeamSets) => {
    if (!selected.size) return setBanner({ kind: 'error', text: t('seam.selectEdgesFirst') });
    setSets((prev) => fn(prev, selected));
  };

  const selectAll = (which: 'seams' | 'protected') =>
    setSelected(new Set(which === 'seams' ? sets.seams : sets.protectedEdges));
  const invertSelection = () => {
    if (!edgeCount) return;
    const next = new Set<number>();
    for (let i = 0; i < edgeCount; i++) if (!selected.has(i)) next.add(i);
    setSelected(next);
  };
  const addEdgeIds = () => {
    const ids = edgeIdInput
      .split(/[\s,]+/)
      .map((s) => parseInt(s, 10))
      .filter((n) => Number.isInteger(n));
    if (!ids.length) return;
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      return next;
    });
    setEdgeIdInput('');
  };

  const replaceWithDraft = () => {
    setSets((prev) => ({ seams: new Set(draft), protectedEdges: prev.protectedEdges }));
    setBanner({ kind: 'info', text: t('seam.draftApplied') });
  };
  const mergeDraft = () =>
    setSets((prev) => ({ seams: new Set([...prev.seams, ...draft]), protectedEdges: prev.protectedEdges }));
  const discardDraft = () => {
    setDraft(new Set());
    setBoundaryReport(null);
  };

  const status = runView?.status?.status ?? null;
  const liveSpec = useMemo(
    () => specFromSets(selectedObject || 'object', sets, { notes: 'Authored in Electron MVP2' }),
    [selectedObject, sets],
  );

  return (
    <>
      <div className="subtoolbar">
        <button disabled={!project} onClick={onInspect}>{t('common.inspect')}</button>
        <button disabled={!project || !selectedObject} className="primary" onClick={onLoadMesh}>
          {t('seam.loadMesh')}
        </button>
        <button disabled={!project || !selectedObject} onClick={onExtractBoundary}>
          {t('seam.extractBoundary')}
        </button>
        <button disabled={!geometry} onClick={onLoadSpec}>{t('seam.loadSpec')}</button>
        <button disabled={!geometry} className="primary" onClick={onSaveSpec}>{t('seam.saveSpec')}</button>
        <button disabled title={t('seam.nextGenerateTitle')}>{t('seam.nextGenerate')}</button>
        {project && <span className="muted small subtoolbar-hint">{project.name}</span>}
      </div>

      <div className="body">
        <SeamLeftPanel
          project={project}
          objects={objects}
          selectedObject={selectedObject}
          onSelectObject={(name) => {
            setSelectedObject(name);
            const obj = objects.find((o) => o.name === name);
            setSelectedLayer(obj?.active_uv_layer ?? obj?.uv_layers[0]?.name ?? '');
          }}
          layers={layers}
          selectedLayer={selectedLayer}
          onSelectLayer={setSelectedLayer}
          overlay={overlay}
          setOverlay={setOverlay}
          tolerance={tolerance}
          setTolerance={setTolerance}
          draft={draft}
          boundaryReport={boundaryReport}
          onReplaceDraft={replaceWithDraft}
          onMergeDraft={mergeDraft}
          onDiscardDraft={discardDraft}
        />

        <main className="center seam-center">
          <div className="seam-edit-toolbar">
            <button onClick={() => apply(markSeam)} className="mark-seam">{t('seam.markSeam')}</button>
            <button onClick={() => apply(markProtected)} className="mark-protect">{t('seam.markProtected')}</button>
            <button onClick={() => apply(clearEdges)}>{t('common.clear')}</button>
            <span className="sep" />
            <button onClick={() => selectAll('seams')}>{t('seam.selectSeams')}</button>
            <button onClick={() => selectAll('protected')}>{t('seam.selectProtected')}</button>
            <button onClick={invertSelection} disabled={!edgeCount}>{t('seam.invert')}</button>
            <button onClick={() => setSelected(new Set())}>{t('seam.deselect')}</button>
            <span className="sep" />
            <input
              className="edge-id-input"
              placeholder={t('seam.edgeIdPlaceholder')}
              value={edgeIdInput}
              onChange={(e) => setEdgeIdInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addEdgeIds()}
            />
            <button onClick={addEdgeIds} title={t('seam.selectIdTitle')}>{t('seam.selectId')}</button>
          </div>

          <SeamViewport
            geometry={geometry}
            seams={sets.seams}
            protectedEdges={sets.protectedEdges}
            selected={selected}
            invalid={invalid}
            conflict={conflict}
            draft={draft}
            overlay={overlay}
            tolerancePx={tolerance}
            onHover={setHovered}
            onPick={pick}
          />
        </main>

        <SeamRightPanel
          selectedObject={selectedObject}
          meshSignature={meshSignature}
          sets={sets}
          selected={selected}
          validation={validation}
          savedValidation={savedValidation}
          activeSpecPath={project?.active_user_seam_spec ?? null}
          onMarkSeam={() => apply(markSeam)}
          onMarkProtected={() => apply(markProtected)}
          onClear={() => apply(clearEdges)}
        />
      </div>

      <SeamBottomPanel runView={runView} status={status} hovered={hovered} spec={liveSpec} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Left panel: project / objects / UV layers / seam specs / edge filters
// ---------------------------------------------------------------------------
function SeamLeftPanel(props: {
  project: Project | null;
  objects: UvObjectSummary[];
  selectedObject: string;
  onSelectObject: (n: string) => void;
  layers: UvLayerInfo[];
  selectedLayer: string;
  onSelectLayer: (n: string) => void;
  overlay: OverlayToggles;
  setOverlay: (o: OverlayToggles) => void;
  tolerance: number;
  setTolerance: (n: number) => void;
  draft: Set<number>;
  boundaryReport: SeamEditorRunView['boundary'] | null;
  onReplaceDraft: () => void;
  onMergeDraft: () => void;
  onDiscardDraft: () => void;
}): JSX.Element {
  const t = useT();
  const { project, overlay, setOverlay } = props;
  const toggle = (k: keyof OverlayToggles) => setOverlay({ ...overlay, [k]: !overlay[k] });
  return (
    <aside className="left">
      <section>
        <h3>{t('common.project')}</h3>
        {project ? (
          <div className="kv">
            <div>{project.name}</div>
            <div className="small">
              {t('common.model')}: <code>{project.working_model ?? project.working_model_fbx ?? project.source_model}</code>
            </div>
          </div>
        ) : (
          <div className="muted">{t('common.noProjectOpen')}</div>
        )}
      </section>

      <section>
        <h3>{t('common.objects')}</h3>
        {props.objects.length ? (
          <ul className="list">
            {props.objects.map((o) => (
              <li
                key={o.name}
                className={o.name === props.selectedObject ? 'sel' : ''}
                onClick={() => props.onSelectObject(o.name)}
              >
                {o.name}
                <span className={`tag ${o.has_uv ? 'ok' : 'unknown'}`}>
                  {o.has_uv ? t('review.tag.uv') : t('review.tag.noUv')}
                </span>
                <div className="muted small">
                  {t('seam.facesEdges', { f: o.faces.toLocaleString(), e: o.edges.toLocaleString() })}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted">{t('prepare.runInspect')}</div>
        )}
      </section>

      <section>
        <h3>{t('review.uvLayers')}</h3>
        {props.layers.length ? (
          <ul className="list">
            {props.layers.map((l) => (
              <li
                key={l.name}
                className={l.name === props.selectedLayer ? 'sel' : ''}
                onClick={() => props.onSelectLayer(l.name)}
              >
                {l.name}
                {l.active && <span className="tag ok">{t('common.tag.active')}</span>}
                {l.empty && <span className="tag unknown">{t('common.tag.empty')}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <div className="muted small">{t('seam.noUvLayer')}</div>
        )}
      </section>

      <section>
        <h3>{t('seam.seamSpecs')}</h3>
        <div className="small">
          {t('seam.activeLabel')}: <code>{project?.active_user_seam_spec ?? t('seam.none')}</code>
        </div>
        {props.draft.size > 0 ? (
          <div className="draftbox">
            <div className="draftbox-head">{t('seam.draftCount', { n: props.draft.size })}</div>
            {props.boundaryReport?.report && (
              <div className="muted small">
                {(props.boundaryReport.report.non_manifold_edges?.length ?? 0) > 0 &&
                  t('seam.nonManifold', { n: props.boundaryReport.report.non_manifold_edges.length })}
                {(props.boundaryReport.report.ambiguous_edges?.length ?? 0) > 0 &&
                  t('seam.ambiguous', { n: props.boundaryReport.report.ambiguous_edges.length })}
                {t('seam.openEdges', { n: props.boundaryReport.report.mesh_boundary_edges?.length ?? 0 })}
              </div>
            )}
            <div className="draftbox-actions">
              <button onClick={props.onReplaceDraft}>{t('seam.replaceSeams')}</button>
              <button onClick={props.onMergeDraft}>{t('seam.merge')}</button>
              <button onClick={props.onDiscardDraft}>{t('seam.discard')}</button>
            </div>
          </div>
        ) : (
          <div className="muted small">{t('seam.extractHint')}</div>
        )}
      </section>

      <section>
        <h3>{t('seam.edgeFilters')}</h3>
        <label className="chk"><input type="checkbox" checked={overlay.showSeams} onChange={() => toggle('showSeams')} /> {t('seam.showSeams')}</label>
        <label className="chk"><input type="checkbox" checked={overlay.showProtected} onChange={() => toggle('showProtected')} /> {t('seam.showProtected')}</label>
        <label className="chk"><input type="checkbox" checked={overlay.showWire} onChange={() => toggle('showWire')} /> {t('seam.showWire')}</label>
        <label className="chk"><input type="checkbox" checked={overlay.showDraft} onChange={() => toggle('showDraft')} /> {t('seam.showDraft')}</label>
        <div className="tol">
          <label className="small">{t('seam.tolerance', { n: props.tolerance })}</label>
          <input type="range" min={3} max={20} value={props.tolerance} onChange={(e) => props.setTolerance(Number(e.target.value))} />
        </div>
      </section>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Right panel: selection / mark buttons / counts / validation / spec metadata
// ---------------------------------------------------------------------------
function SeamRightPanel(props: {
  selectedObject: string;
  meshSignature: MeshSignature | null;
  sets: SeamSets;
  selected: Set<number>;
  validation: SeamValidation;
  savedValidation: SeamValidation | null;
  activeSpecPath: string | null;
  onMarkSeam: () => void;
  onMarkProtected: () => void;
  onClear: () => void;
}): JSX.Element {
  const t = useT();
  const sig = props.meshSignature;
  const { validation } = props;
  return (
    <aside className="right">
      <section>
        <h3>{t('seam.selection')}</h3>
        <div className="kv">
          <div className="big">{props.selected.size}</div>
          <div className="muted small">{t('seam.edgesSelected')}</div>
        </div>
        <div className="markrow">
          <button className="mark-seam" onClick={props.onMarkSeam}>{t('seam.markSeam')}</button>
          <button className="mark-protect" onClick={props.onMarkProtected}>{t('seam.protect')}</button>
          <button onClick={props.onClear}>{t('common.clear')}</button>
        </div>
      </section>

      <section>
        <h3>{t('seam.counts')}</h3>
        <table className="metrics">
          <tbody>
            <tr><td>{t('seam.row.seamEdges')}</td><td><span className="dot seam" /> {props.sets.seams.size}</td></tr>
            <tr><td>{t('seam.row.protectedEdges')}</td><td><span className="dot protect" /> {props.sets.protectedEdges.size}</td></tr>
            <tr><td>{t('seam.row.selected')}</td><td>{props.selected.size}</td></tr>
            <tr><td>{t('seam.row.invalid')}</td><td className={validation.invalid_edges.length ? 'err' : ''}>{validation.invalid_edges.length}</td></tr>
            <tr><td>{t('seam.row.conflicts')}</td><td className={validation.conflicts.length ? 'warn' : ''}>{validation.conflicts.length}</td></tr>
          </tbody>
        </table>
      </section>

      <section>
        <h3>{t('common.validation')}</h3>
        {validation.invalid_edges.length === 0 && validation.conflicts.length === 0 ? (
          <div className="ok small">{t('seam.specClean')}</div>
        ) : (
          <ul className="issuelist">
            {validation.invalid_edges.length > 0 && (
              <li className="error">
                <span className="sevdot error" /> {t('seam.outOfRange', { n: validation.invalid_edges.length })}
                <code className="small"> {validation.invalid_edges.slice(0, 8).join(', ')}{validation.invalid_edges.length > 8 ? '…' : ''}</code>
              </li>
            )}
            {validation.conflicts.map((c) => (
              <li key={c.edge_id} className="warning">
                <span className="sevdot warning" /> {t('seam.conflictRow', { id: c.edge_id })}
              </li>
            ))}
          </ul>
        )}
        {props.savedValidation?.object_mismatch && (
          <div className="err small">{t('seam.objMismatch')}</div>
        )}
      </section>

      <section>
        <h3>{t('seam.meshSpec')}</h3>
        <table className="metrics">
          <tbody>
            <tr><td>{t('seam.row.object')}</td><td>{props.selectedObject || '—'}</td></tr>
            <tr><td>{t('common.vertices')}</td><td>{sig ? sig.vertices.toLocaleString() : '—'}</td></tr>
            <tr><td>{t('common.edges')}</td><td>{sig ? sig.edges.toLocaleString() : '—'}</td></tr>
            <tr><td>{t('common.faces')}</td><td>{sig ? sig.faces.toLocaleString() : '—'}</td></tr>
            <tr><td>{t('seam.row.activeSpec')}</td><td className="small"><code>{props.activeSpecPath ?? '—'}</code></td></tr>
          </tbody>
        </table>
      </section>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Bottom: hovered edge / run status / logs / raw spec preview
// ---------------------------------------------------------------------------
type BottomTab = 'spec' | 'logs';

function SeamBottomPanel(props: {
  runView: SeamEditorRunView | null;
  status: string | null;
  hovered: number | null;
  spec: SeamSpec;
}): JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<BottomTab>('spec');
  const rv = props.runView;
  return (
    <footer className="bottom">
      <div className="statusrow">
        <span className={`statuspill ${props.status ?? 'idle'}`}>{statusLabel(t, props.status)}</span>
        <span className="muted small">
          {t('seam.hoverEdge')}: <code>{props.hovered ?? '—'}</code>
        </span>
        {rv?.status?.error && (
          <span className="err">{rv.status.error.code}: {rv.status.error.message}</span>
        )}
      </div>
      <div className="reporttabs">
        <nav className="tabbar">
          <button className={tab === 'spec' ? 'active' : ''} onClick={() => setTab('spec')}>{t('seam.tab.spec')}</button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('common.tab.logs')}</button>
        </nav>
        <div className="tabbody">
          {tab === 'spec' && <pre className="json">{JSON.stringify(props.spec, null, 2)}</pre>}
          {tab === 'logs' && (
            <div className="logs">
              <div className="logcol">
                <h4>stdout</h4>
                <pre>{rv?.stdout || t('common.empty')}</pre>
              </div>
              <div className="logcol">
                <h4>stderr</h4>
                <pre className={rv?.stderr ? 'err' : ''}>{rv?.stderr || t('common.empty')}</pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </footer>
  );
}
