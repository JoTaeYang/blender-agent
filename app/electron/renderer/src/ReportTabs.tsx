import React, { useState } from 'react';
import type { RunView } from '@shared/contracts';

type TabKey = 'summary' | 'generation' | 'topology' | 'shape' | 'logs';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'summary', label: 'Summary' },
  { key: 'generation', label: 'Generation' },
  { key: 'topology', label: 'Topology' },
  { key: 'shape', label: 'Shape' },
  { key: 'logs', label: 'Logs' },
];

export function ReportTabs(props: { runView: RunView | null }): JSX.Element {
  const [tab, setTab] = useState<TabKey>('summary');
  const rv = props.runView;

  return (
    <div className="reporttabs">
      <nav className="tabbar">
        {TABS.map((t) => (
          <button key={t.key} className={t.key === tab ? 'active' : ''} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </nav>
      <div className="tabbody">
        {!rv && <div className="muted">No run selected.</div>}
        {rv && tab === 'summary' && <Json data={rv.summary} empty="No summary yet." />}
        {rv && tab === 'generation' && <Json data={rv.reports['generation_report']} empty="No generation report." />}
        {rv && tab === 'topology' && <Json data={rv.reports['validation_report']} empty="No topology report." />}
        {rv && tab === 'shape' && <Json data={rv.reports['shape_report']} empty="No shape report." />}
        {rv && tab === 'logs' && <Logs rv={rv} />}
      </div>
    </div>
  );
}

function Json(props: { data: unknown; empty: string }): JSX.Element {
  if (props.data === null || props.data === undefined) {
    return <div className="muted">{props.empty}</div>;
  }
  return <pre className="json">{JSON.stringify(props.data, null, 2)}</pre>;
}

function Logs(props: { rv: RunView }): JSX.Element {
  const { stdout, stderr } = props.rv;
  return (
    <div className="logs">
      <div className="logcol">
        <h4>stdout</h4>
        <pre>{stdout || '(empty)'}</pre>
      </div>
      <div className="logcol">
        <h4>stderr</h4>
        <pre className={stderr ? 'err' : ''}>{stderr || '(empty)'}</pre>
      </div>
    </div>
  );
}
