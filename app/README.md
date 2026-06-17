# UV Review App — MVP 0

Electron desktop app for the high-to-low preparation workflow
(`docs/ELECTRON_UV_REVIEW_APP_MVP0_PRODUCTION_PLAN.ko.md`): import a source model,
inspect it, generate a low-poly candidate, review reports + preview, and approve a
working low-poly that the next MVP reads from `project.json`.

## Architecture

```
app/
  shared/contracts/        TS contract (mirror of worker/app_job_contract.py)
  electron/
    main/                  project folder service, IPC, Blender worker runner
    preload/               context-isolated window.api bridge
    renderer/              React UI (project shell, reports, preview, approve)
  test/                    main-process integration smoke (mock runner)
```

The renderer never touches the filesystem or spawns Blender — it calls
`window.api.*`, which forwards to the main process over IPC. The main process owns
the project folder, spawns the Python/Blender workers in `../worker`, and reads back
JSON artifacts (`status.json`, `summary.json`, per-phase reports).

## Develop

```bash
npm install
npm run dev          # launch the app (Vite + Electron)
npm run typecheck    # tsc for main+preload (node) and renderer (web)
npm run build        # production bundle
npm run test:integration   # create->inspect->generate(mock)->approve, no Blender
```

## Blender

Set the Blender executable path in the app's top settings bar (auto-detected on
common install paths). Without it, generation falls back to a **mock runner** that
fabricates a deterministic accepted run so the UI flow is fully exercisable.

The underlying workers:

- `../worker/inspect_model.py` — import + mesh summary + role hint
- `../worker/run_app_retopo_job.py` — low-poly generation + status/summary lifecycle
