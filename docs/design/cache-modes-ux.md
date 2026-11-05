# Cache modes: tc-os editor UX

Design only. Nothing here is implemented in tc-os in this release (PRD Must 10).

Inputs: [plan](cache-modes-plan.md), [PRD](cache-modes-prd.md), [codebase §5](cache-modes-codebase.md),
[prior art](cache-modes-competitors.md). tc-os was read at `~/Developer/tc-os` (main, b712a32c). Frontend paths
below are under `frontend/src/`.

## Summary

- Workflow default: a **Cache check** submenu in the header menu, under "Run options", next to "Max parallel
  tasks" and "Stop the run when a task fails". Two radio items, Deep and Fast. Deep is stored as no field.
- Task override: a **Cache check** select in the block guide's first step ("Task kind", whose blurb already says
  "Name, description and caching"). Three options: Workflow default (shows the current value), Deep, Fast.
- Effective mode is shown in three places that already show caching: the task's cache dock tooltip, the phone
  sheet's "Reuse cache" button description, and a new "Check" column in the Tasks panel.
- No new primitives. Every control is an existing shadcn/Radix piece already in `components/ui/`.

## How settings are edited today

| Setting | Where | How | Code |
|---|---|---|---|
| `failure_policy` | Header "..." menu, "Run options" | `DropdownMenuCheckboxItem`; default stored as no field | `components/WorkflowList/WorkflowHeader.tsx:118, 593-633`; `hooks/workflow/WorkflowContext.tsx:1077` (`setWorkflowField`), `:2203` |
| `max_concurrency` | Same group | Menu item with the value on the right, opens `ConcurrencyDialog` | `WorkflowHeader.tsx:606-617`; `components/Workflow/ConcurrencyDialog.tsx` |
| `orchestrator_target` | No editor UI | Chat / MCP `set_workflow_meta` only | `backend/src/app/core/chat/draft.py:437` |
| `skip_if_complete` | Task cache dock on the canvas (not the guide) | Toggle button, `DatabaseZap`, `aria-pressed`; bulk in the selection toolbar; "Reuse cache" on phones | `components/Canvas/ReactFlow/TaskNode.tsx:266-300`, `SelectionToolbar.tsx:129-157`, `NodeSheet.tsx:498-517`, `blockActions.ts:149-197` |
| `resources`, `secrets`, runtime/executor/target params | Block guide steps | Hand-written editors; plugin kind params through `KindParamsEditor` -> `SchemaForm` | `components/Canvas/BlockGuide/BlockGuide.tsx`, `BlockGuide/types.ts:25` (`STEPS`), `components/Canvas/SchemaForm.tsx` |
| Anything | YAML panel | Monaco; Apply validates through the orchestrator, then saves | `components/Canvas/Yaml/YamlPanel.tsx:60-90` |

Read-only: in run view (`isDesignMode` false) the header hides workflow settings, the guide wraps every control in
a disabled `<fieldset>` (`BlockGuide.tsx:569`), and the cache dock button is disabled. Map clones and inlined
subworkflow tasks are `derived` and read-only.

i18n: the tc-os frontend has none. Every string is an English literal in the component (for example the `ACTIONS`
table in `WorkflowHeader.tsx:102`). Spanish copy below is for the runtime message, which tc-os shows verbatim, and
for when the frontend gets i18n. It is not a reason to add an i18n library in this change.

Backend storage: `PATCH /workflows/{id}` stores `workflow_data` as an opaque dict. It calls the orchestrator
validator only to find secret paths and does not reject on errors (`backend/src/app/api/routes/workflow.py:679-700`).
Only the YAML panel's Apply and the validate route reject a bad document. So a bad `cache:` value can be stored
(import, MCP) and has to render sanely.

## Does the generic schema form render it with zero code?

No.

- `SchemaForm` renders **plugin kind params** only (runtime, executor, target, artifact limits). `BaseTask` and
  `BaseWorkflow` fields are hand-written. `TaskInfo.json_schema` reaches the editor (`HorusKindsResponse.tasks`)
  but nothing renders it. There is no workflow schema in `HorusKindsResponse` at all.
- Even if it were routed there, `SchemaForm`'s enum branch (`SchemaForm.tsx:329`) would fall short:
  - it shows raw values (`deep`, `fast`), with no labels or help per option;
  - `unwrap` drops the `null` branch, so there is no "inherit" option and no way back to unset;
  - an unknown value renders as a blank trigger with no error;
  - it cannot show the inherited value.

What does work with zero code once the vendored runtime is bumped:

- Typing `cache: fast` in the YAML panel, at either level. Apply validates it, and an invalid value is rejected
  with the runtime's `WorkflowError` text in the error box (`YamlPanel.tsx:193`).
- A task's `cache:` survives block guide edits. `cache` is not in `TASK_STANDARD_FIELDS`
  (`hooks/workflow/TaskUtils.ts:1419`), so it rides along as a kind param (`kindParamsOf`, `withKindParams`).
- Runs, map clones and loop bodies pick it up in the runtime.

What is missing:

- Any visible control or display.
- **Bug-in-waiting:** switching a task's kind in the guide resets kind params (`BlockGuide.tsx:474`,
  `setKindParams({})`), which silently drops `cache`. Fixed by making `cache` a standard field (below).
- `types.gen.ts` has no `cache` on `BaseTask`/`BaseWorkflow` until the OpenAPI client is regenerated from the
  bumped runtime. The index signatures make it compile either way.
- Chat and MCP cannot set it: `TASK_AUTHORING_FIELDS` and `_set_workflow_meta` whitelist fields
  (`backend/src/app/core/chat/draft.py:37, 437`). Backend, listed for completeness.

## User flows

### F1. Set the workflow default

1. Open the header "..." menu. "Run options" shows **Cache check** with the current value on the right (`Deep`).
2. Open the submenu (click, tap, Enter or ArrowRight). Pick **Fast**.
3. The menu closes and the change saves right away, as "Stop the run when a task fails" does. Toast:
   `Cache check set to Fast. Tasks that inherit it re-run once.`
4. Choosing Deep removes the `cache` key from the document (Deep is the runtime default).

### F2. Override one task

1. Open the task (double-click, or Edit). The guide opens on "Task kind".
2. Under Name/Description, **Cache check** shows `Workflow default (Fast)`.
3. Pick **Deep**. A note appears under the select: `Saving re-runs this task once.`
4. Save. The task document gets `cache: deep`.

### F3. Reset a task to inherit

1. Open the task. **Cache check** shows `Deep`, with `Overrides the workflow default (Fast).` under it.
2. Pick **Workflow default (Fast)**. Save. The `cache` key is removed from the task.

No separate reset button: the inherit option is the reset, as an empty box is for `max_concurrency`.

### F4. See the effective mode

- Hover or focus the task's cache dock: the tooltip gets a second line, `Fast check (workflow default)` or
  `Deep check (set on this task)`.
- Phone sheet: the "Reuse cache" button's `aria-description` gets the same line. The guide's first step shows it
  visibly.
- Tasks panel: a **Check** column, `Deep` / `Fast`, with `(task)` when overridden.
- Run view: the same three places read the run's snapshot, so they show the mode that run used.

## Wireframes

### W1. Header menu, design mode (desktop and phone)

```
 ┌───────────────────────────────────────────┐
 │ Run options                               │
 │  ▶  Run from task                         │
 │  ↻  Force re-run all tasks            [ ] │
 │  ◔  Max parallel tasks                  8 │
 │  ⊘  Stop the run when a task fails    [x] │
 │  ⌘  Cache check                  Deep  ›  │──┐
 │  🐞 Debug logging                     [ ] │  │ ┌─────────────────────────────────┐
 ├───────────────────────────────────────────┤  └─│ (•) Deep                         │
 │ Share and export                          │    │     Hash file contents           │
 │  ...                                      │    │ ( ) Fast                         │
 └───────────────────────────────────────────┘    │     Compare size and date        │
                                                  └─────────────────────────────────┘
```

Icon: lucide `Fingerprint` (the check builds the task fingerprint). The value on the right uses the same
`ml-auto text-muted-foreground` span as "Max parallel tasks". Hidden in run view, like its neighbours.

Invalid value loaded from YAML (`cache: quick`):

```
 │  ⌘  Cache check              ⚠ quick  ›  │──┐ ┌─────────────────────────────────┐
                                                └─│ Unknown value "quick".           │
                                                  │ ( ) Deep                         │
                                                  │ ( ) Fast                         │
                                                  └─────────────────────────────────┘
```

No radio is checked. The value is `text-destructive` with `AlertTriangle`, so it does not rely on colour.
Picking either one fixes the document.

### W2. Block guide, "Task kind" step

Default, inherited:

```
 ┌ Task kind ──────────────────────────────────────────────────────────┐
 │ [ shell ][ python ][ horus_map ] ...                                │
 │                                                                     │
 │ Name                          Description                           │
 │ [ align_reads             ]   [ What this task does (optional) ]    │
 │                                                                     │
 │ Cache check                                                         │
 │ [ Workflow default (Fast)                                   ▾ ]     │
 │ Fast compares size and modified time. Files under 1 MiB are hashed. │
 └─────────────────────────────────────────────────────────────────────┘
```

Open:

```
 │ [ Workflow default (Fast)                                   ▾ ]     │
 │ ┌───────────────────────────────────────────────┐                   │
 │ │ ✓ Workflow default (Fast)                     │                   │
 │ │   Deep                                        │                   │
 │ │   Fast                                        │                   │
 │ └───────────────────────────────────────────────┘                   │
```

Overridden, changed in this session:

```
 │ Cache check                                                         │
 │ [ Deep                                                      ▾ ]     │
 │ Overrides the workflow default (Fast).                              │
 │ Saving re-runs this task once.                                      │
```

Invalid value loaded from YAML:

```
 │ Cache check                                                         │
 │ [ Unknown: quick                                            ▾ ]  ← aria-invalid
 │ ⚠ Pick Deep, Fast or the workflow default.                          │
```

Rendered as `value=""` with the placeholder `Unknown: quick`, so Radix shows text instead of a blank trigger.
Save stays enabled. The guide does not rewrite the value unless the user picks one, matching how
`unitlessMemoryWarning` never rewrites the typed value.

Cache off for this task (`skip_if_complete: false`) or "Force re-run all tasks" on: the select stays enabled
(the user can preset it) and the help line becomes `Cache is off for this task. This applies when it is on.`

Read-only (run view, derived task): the same select, disabled by the guide's fieldset, showing the snapshot value.
No "Saving re-runs" note.

### W3. Task cache dock tooltip

```
            ┌──────────────────────────────────────────────┐
            │ Reusing cached results when inputs are       │
            │ unchanged                                    │
            │ Fast check (workflow default)                │
            └──────────────────────────────────────────────┘
                         [⛁]  ← DatabaseZap, emerald when on
 ┌ align_reads ─────────────────────────────────┐
```

No new badge on the card. The mode matters only when caching is on, and the dock already stands for caching.
When cache is off or force re-run is on, the tooltip keeps its current single line.

### W4. Tasks panel

```
 Name          Status     Target   Runtime  ...  Duration  Cache  Check
 align_reads   completed  slurm    shell         12m       On     Fast
 markdup       skipped    slurm    shell         -         On     Deep (task)
 qc_report     idle       local    python        -         Off    Fast
 bad_one       idle       local    shell         -         On     ⚠ quick
```

## States

| State | Workflow menu | Guide select | Dock tooltip line 2 | Tasks panel |
|---|---|---|---|---|
| Default (no `cache` anywhere) | `Deep`, Deep checked | `Workflow default (Deep)` | `Deep check (workflow default)` | `Deep` |
| Inherited from a fast workflow | `Fast` | `Workflow default (Fast)` | `Fast check (workflow default)` | `Fast` |
| Overridden on the task | unchanged | `Deep` + "Overrides the workflow default (Fast)." | `Deep check (set on this task)` | `Deep (task)` |
| Invalid value in YAML | `⚠ quick`, nothing checked | placeholder `Unknown: quick`, error line | `Unknown cache check "quick"` | `⚠ quick` |
| Read-only run view | hidden (as `failure_policy`) | disabled, snapshot value | same as design mode | same |
| Derived task (map clone, subworkflow body) | n/a | disabled, parent's effective value | inherited value | inherited value |

## Copy

Sentence case. Menu items and options have no final period; help and errors end with one. The terms Deep and
Fast match the YAML values so readers can map the UI to `cache: deep | fast`.

| Key | English | Spanish |
|---|---|---|
| Menu item / field label | Cache check | Comprobación de caché |
| Option | Deep | Profunda |
| Option hint | Hash file contents | Calcula el hash del contenido |
| Option | Fast | Rápida |
| Option hint | Compare size and date | Compara tamaño y fecha |
| Inherit option | Workflow default ({mode}) | Del flujo de trabajo ({mode}) |
| Help, inherit/fast | Fast compares size and modified time. Files under 1 MiB are hashed. | Rápida compara tamaño y fecha de modificación. Los archivos de menos de 1 MiB se comprueban por contenido. |
| Help, deep | Deep hashes the contents of every input. | Profunda calcula el hash del contenido de cada entrada. |
| Override note | Overrides the workflow default ({mode}). | Sustituye el valor del flujo de trabajo ({mode}). |
| Change note | Saving re-runs this task once. | Al guardar, esta tarea se vuelve a ejecutar una vez. |
| Cache off note | Cache is off for this task. This applies when it is on. | La caché está desactivada en esta tarea. Se aplica cuando esté activada. |
| Toast, workflow change | Cache check set to {mode}. Tasks that inherit it re-run once. | Comprobación de caché: {mode}. Las tareas que la heredan se vuelven a ejecutar una vez. |
| Tooltip, inherited | {mode} check (workflow default) | Comprobación {mode} (del flujo de trabajo) |
| Tooltip, override | {mode} check (set on this task) | Comprobación {mode} (definida en esta tarea) |
| Tasks column header | Check | Comprobación |
| Override suffix | (task) | (tarea) |
| Invalid, placeholder | Unknown: {value} | Desconocido: {value} |
| Invalid, error line | Pick Deep, Fast or the workflow default. | Elige Profunda, Rápida o el valor del flujo de trabajo. |
| Invalid, menu | Unknown value "{value}". | Valor desconocido «{value}». |
| Runtime error (horus-runtime `_()`, shown verbatim in the YAML panel) | Invalid cache mode "{value}". Use deep or fast. | Modo de caché no válido «{value}». Usa deep o fast. |

The runtime error keeps `deep`/`fast` untranslated in both languages, because they are YAML values. The
runtime string itself is owned by horus-runtime (PRD Must 7); the row above is a proposal for it.

## Keyboard and accessibility

- Menu: Radix `DropdownMenuSub` + `DropdownMenuRadioGroup` (`components/ui/dropdown-menu.tsx:249-256`). Enter,
  Space or ArrowRight opens the submenu; ArrowLeft closes it; Up/Down move; Enter or Space picks. Items get
  `role="menuitemradio"` and `aria-checked` from Radix. The option hint sits inside the item, so it is part of
  the accessible name.
- Guide: Radix `Select` with a `Label htmlFor`. Typeahead works on W, D and F. Help, override and change notes
  are tied with `aria-describedby`; the invalid state sets `aria-invalid` and points `aria-describedby` at the
  error line, as the Contents box does (`BlockGuide.tsx:666-672`).
- Radix forbids an empty item value, so the inherit option uses a sentinel (`"inherit"`), mapped to "no key".
- The guide sits in a dialog, but React Flow listens on `document` for Space. Check that Space opens the select
  and does not pan; other selects in the same dialog (`KindParamsEditor`) suggest it is fine.
- State is never colour-only: invalid shows `⚠` and text; overrides show "(task)" or the override note.
- Tooltips are not the only carrier. On touch, the guide step and the Tasks panel show the same information as
  text, and the phone sheet button carries it in `aria-description`.
- Touch targets: phone menu rows and the guide select already use the house sizes (`h-11` sheet buttons,
  `pointer-coarse:` sizes). Nothing smaller is added.

## Reused components

| Need | Reuse | Change |
|---|---|---|
| Workflow setting in the menu | "Run options" group, `ACTIONS` table, `ActionIcon` (`WorkflowHeader.tsx:102-126, 593-640`) | New `cacheCheck` entry; `DropdownMenuSub`/`SubTrigger`/`SubContent` + `RadioGroup`/`RadioItem` from `components/ui/dropdown-menu.tsx` (exported, not yet used in the app) |
| Save a workflow field, default as no key | `setWorkflowField` (`WorkflowContext.tsx:1077`) | Add `"cache"` to its key union; `setCacheMode(mode)` writes `null` for `deep`, as `setFailurePolicy` does for `fail_fast` |
| Task field in the guide | `Select*` from `components/ui/select.tsx`, `Label`, help line style `text-[11px] text-muted-foreground`, error style `text-[11px] text-destructive` | None |
| Task round-trip | `createTask`, `taskToSelection`, `updateTaskFromSelection`, `TASK_STANDARD_FIELDS` (`TaskUtils.ts:435, 464, 628, 1419`) | Add `cache` as a standard field |
| Effective mode display | Cache dock tooltip (`TaskNode.tsx:266-300`), sheet button (`NodeSheet.tsx:498-517`), Tasks panel `taskRow` + `columns` (`Tasks/taskRows.ts:41`, `Tasks/columns.tsx:93`) | Second tooltip line, `aria-description` text, one new column |
| Error toast | `useCustomToast` + `handleError` as the `failure_policy` item uses | None |

Not reused, on purpose:

- `SchemaForm`: the field is a `BaseTask`/`BaseWorkflow` field, not a kind param, and the form lacks inherit and
  unknown-value states (see above).
- `SegmentedControl`: nicer for two values, but it cannot show "inherit" plus an unknown value, and the guide's
  other pick-one fields are `Select`s.
- A checkbox like "Stop the run when a task fails": it cannot show an unknown value, and a third mode later
  (Nextflow has `lenient`) would mean redoing it.

## Data flow

```
horus-runtime                    orchestrator / backend                    frontend
─────────────                    ──────────────────────                    ────────
BaseWorkflow.cache: Literal      vendored runtime -> OpenAPI ───regen───▶  types.gen.ts
  ["deep","fast"] = "deep"         (BaseWorkflow, BaseTask)                  BaseWorkflow.cache?, BaseTask.cache?
BaseTask.cache: Literal | None   AutoRegistry json_schema ──/horus/kinds─▶ TaskInfo.json_schema (not rendered)

                                                                           Header submenu ─▶ setCacheMode
                                                                           Guide select  ─▶ TaskSelection.cache
                                                                                │             └▶ create/updateTask
                                                                                ▼
                                 PATCH /workflows/{id} {workflow_data} ◀── store.save()
                                 stored as-is (opaque dict); validator         ▲
                                 only used for secret paths                    │
                                                                           YAML panel Apply ─▶ POST validate
                                 orchestrator validates with runtime ──────▶   (rejects bad cache:, runtime message)
                                                                               then save()
                                 GET .../yaml  (yaml.safe_dump) ◀── same doc ─▶ dumpWorkflowYaml (js-yaml)
```

Write rules:

- Workflow: `deep` -> delete the key; `fast` -> `cache: fast`. Unknown values are kept until the user picks.
- Task: inherit -> delete the key; `deep` / `fast` -> set it. Same as `secrets`, which is dropped when empty.
- Effective mode, one helper in `TaskUtils.ts`: `task.cache ?? workflow.cache ?? "deep"`. Derived tasks read
  their parent map's value. Subworkflow bodies depend on D4 (open question 2).

## Frontend files likely touched

| File | Change |
|---|---|
| `client/types.gen.ts` (plus `frontend/openapi.json` and the root `openapi.json`) | Regenerated after the vendored runtime bump |
| `hooks/workflow/WorkflowContext.tsx` | `setWorkflowField` key union; `setCacheMode` |
| `components/WorkflowList/WorkflowHeader.tsx` | `ACTIONS.cacheCheck`; submenu in "Run options"; toast |
| `components/Canvas/BlockGuide/types.ts` | `TaskSelection.cache?: "deep" \| "fast" \| null` |
| `components/Canvas/BlockGuide/BlockGuide.tsx` | Select in the "task" step; state kept across task-kind switches |
| `hooks/workflow/TaskUtils.ts` | `cache` in `TASK_STANDARD_FIELDS`; create/select/update mapping; `effectiveCache` helper |
| `components/Canvas/ReactFlow/TaskNode.tsx` | Tooltip line 2 |
| `components/Canvas/NodeSheet.tsx` | `aria-description` text |
| `components/Canvas/Tasks/taskRows.ts`, `Tasks/columns.tsx` | `check` field and column |
| Tests: `hooks/workflow/resources.test.ts` (round-trip, like `skip_if_complete`), `Tasks/taskRows.test.ts` | Inherit/override/invalid cases; kind switch keeps `cache` |

Backend, outside the frontend but needed for parity: `backend/src/app/core/chat/draft.py` (`TASK_AUTHORING_FIELDS`,
`_set_workflow_meta` with the same enum check as `failure_policy`) and `core/chat/tools.py` (tool schema), which
also serve the Horus MCP `update_task` and `set_workflow_meta`.

## Out of scope for v1

- Bulk override from the selection toolbar. Use the YAML panel for many tasks.
- A badge on the task card.
- Explaining why a task re-ran (PRD Won't: per-miss reasons).
- A frontend i18n library.

## Open questions

1. Name: "Cache check" next to the existing "Reuse cached results" / "Cache" column, or "Input check"? The first
   ties the two; the second says what is compared.
2. Subworkflow bodies (PRD D4): if a body's `cache:` becomes its tasks' default, the inherit option for those
   tasks must read `Subworkflow default ({mode})`. tc-os inlines bodies as derived tasks; is the body's
   workflow-level field even kept in the stored document?
3. Should Fast be offered in tc-os at all before tc-os records fast digests and keeps root-input mtimes (PRD
   Risk 2, open question 7)? If not, show Fast disabled with `Not available on Temple Compute yet.` rather than
   hide it, so YAML-authored values still render.
4. Run view: show the workflow default somewhere (the header hides all run options in run view today), or is the
   per-task display enough?
5. The workflow toast says "re-run once" for every inheriting task. Worth counting them (`{n} tasks re-run
   once`)? It is cheap: inheriting tasks with at least one input.
6. Radix submenus on a 360 px phone: the header menu is already long. Check that collision handling keeps the
   submenu on screen, or fall back to inline radio rows on phones.
7. Should the PATCH route reject an invalid `cache:` instead of storing it? Today it stores any document
   (`workflow.py:679-700`), which is why the invalid state exists.
