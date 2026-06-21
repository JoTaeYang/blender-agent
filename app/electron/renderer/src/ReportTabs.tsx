import React, { useState } from 'react';
import type { RunView } from '@shared/contracts';
import { useT, type TKey } from './i18n';

type TabKey = 'summary' | 'generation' | 'topology' | 'shape' | 'logs';

const TABS: { key: TabKey; label: TKey }[] = [
  { key: 'summary', label: 'common.tab.summary' },
  { key: 'generation', label: 'prepare.tab.generation' },
  { key: 'topology', label: 'prepare.tab.topology' },
  { key: 'shape', label: 'prepare.tab.shape' },
  { key: 'logs', label: 'common.tab.logs' },
];

export function ReportTabs(props: { runView: RunView | null }): JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<TabKey>('summary');
  const rv = props.runView;

  return (
    <div className="reporttabs">
      <nav className="tabbar">
        {TABS.map((item) => (
          <button
            key={item.key}
            className={item.key === tab ? 'active' : ''}
            onClick={() => setTab(item.key)}
          >
            {t(item.label)}
          </button>
        ))}
      </nav>
      <div className="tabbody">
        {!rv && <div className="muted">{t('reporttabs.noRun')}</div>}
        {rv && tab === 'summary' && <Json data={rv.summary} empty={t('common.noSummary')} />}
        {rv && tab === 'generation' && (
          <Json data={rv.reports['generation_report']} empty={t('prepare.noGenReport')} />
        )}
        {rv && tab === 'topology' && (
          <Json data={rv.reports['validation_report']} empty={t('prepare.noTopoReport')} />
        )}
        {rv && tab === 'shape' && (
          <Json data={rv.reports['shape_report']} empty={t('prepare.noShapeReport')} />
        )}
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
  const t = useT();
  const { stdout, stderr } = props.rv;
  return (
    <div className="logs">
      <div className="logcol">
        <h4>stdout</h4>
        <pre>{stdout || t('common.empty')}</pre>
      </div>
      <div className="logcol">
        <h4>stderr</h4>
        <pre className={stderr ? 'err' : ''}>{stderr || t('common.empty')}</pre>
      </div>
    </div>
  );
}
