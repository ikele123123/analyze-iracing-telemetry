---
name: analyze-iracing-telemetry
description: Analyze and compare iRacing telemetry from a player IBT file and an expert/reference CSV, then generate a highly visual offline HTML report with racing lines, per-corner speed, throttle, brake, ABS, gear, steering, G-force, time loss, and actionable coaching. Use when the user mentions iRacing telemetry, .ibt recordings, Garage 61 CSV exports, lap comparison, racing-line analysis, braking or ABS analysis, or requests a reusable per-corner telemetry report for any car or track.
---

# Analyze iRacing Telemetry

Generate a reproducible, interactive Chinese HTML debrief from one player IBT and one expert CSV. Use the bundled analyzer instead of rewriting telemetry parsing or report code.

## Workflow

1. Identify the player `.ibt`, expert `.csv`, work directory, and desired output path.
2. If more than one candidate exists, select files from the user's request; do not guess between multiple sessions or references.
3. Ensure Python can import `irsdk`, `numpy`, `pandas`, `scipy`, `plotly`, and `yaml`. Install missing packages only when needed: `pyirsdk numpy pandas scipy plotly pyyaml`.
4. Run `scripts/analyze_telemetry.py` with explicit UTF-8 environment settings.
5. Review the printed lap selection, lap-time gap, turn count, detected corner centers, and top losses.
6. Open the HTML in a real browser and verify the track, T-number placement, per-corner selector, six telemetry layers, ABS pulses, desktop layout, and a narrow mobile viewport.
7. Read [references/interpretation.md](references/interpretation.md) before writing the coaching summary or changing recommendation rules.
8. Return a clickable HTML path, the main quantified findings, limitations, and the exact command used.

## Run The Analyzer

When the work directory contains exactly one IBT and one CSV under the root or `references/`:

```powershell
python scripts/analyze_telemetry.py --workdir "E:\telemetry-session"
```

For explicit inputs:

```powershell
python scripts/analyze_telemetry.py `
  --ibt "E:\telemetry-session\player.ibt" `
  --reference "E:\telemetry-session\references\expert.csv" `
  --output "E:\telemetry-session\telemetry_analysis_report.html"
```

Useful overrides:

- `--reference-time 1:35.229`: use when the reference filename has no `MM.SS.mmm` time.
- `--corner-count 14`: override missing or unsuitable `TrackNumTurns` metadata.
- `--corner-centers 0.11,0.20,0.28`: override automatic detection with ordered `LapDistPct` values.
- Percent notation is also accepted: `--corner-centers 11%,20%,28%`.

In PowerShell, set console and Python UTF-8 before commands and use explicit UTF-8 when reading text files.

## Turn Detection

Default to `WeekendInfo.TrackNumTurns` and identify that number of expert steering peaks. Treat this as a heuristic, not official corner truth.

Always inspect the track figure after automatic detection. Rerun with `--corner-centers` when:

- a light bend is omitted;
- one long corner produces multiple peaks;
- an S-complex is numbered differently from the official map;
- a T label or analysis window points at the wrong feature;
- the start/finish location causes a wrapped corner window.

Keep turn centers in increasing lap-progress order. Do not silently edit the official turn count to make a chart look cleaner.

## Report Contract

Preserve these conventions unless the user explicitly asks otherwise:

- Use red solid lines for the player and green dashed lines for the expert everywhere.
- Place T labels away from the track with leader lines.
- Keep one per-corner interactive analysis instead of duplicating a full-lap telemetry panel.
- Label the six left-side telemetry layers: speed, brake, throttle, steering, gear, and ABS.
- Include per-turn racing line, entry/minimum/average speed, braking point/peak/duration, ABS events/duration/assessment, throttle commitment, gear, steering, G-force, time loss, and coaching.
- Generate a standalone offline HTML file with Plotly embedded once.
- State that reference fuel, setup, tires, and weather may be unknown.

## Validation

Run syntax and skill checks:

```powershell
python -m py_compile scripts/analyze_telemetry.py
python <skill-creator>/scripts/quick_validate.py <this-skill-directory>
```

Browser validation must confirm:

- no page-level horizontal overflow at desktop and approximately 390 px width;
- no JavaScript page errors;
- player/expert traces use only the two contracted styles;
- every T button updates the title, local map, telemetry, metrics, ABS, and advice;
- graph pixels are nonblank and metric rows are not covered by Plotly containers.

Do not keep screenshots, test reports, or `__pycache__` in the user's work directory after validation.
