# iRacing Telemetry Analyzer Skill

A reusable Codex skill that compares a player's iRacing `.ibt` telemetry with an expert reference `.csv` and produces a standalone interactive HTML debrief.

## Features

- Automatically selects the best complete, clean player lap.
- Aligns both laps by `LapDistPct` and compares GPS racing lines.
- Reads track, car, lap time, and turn count from iRacing metadata.
- Detects turn centers from expert steering peaks, with manual overrides for complex layouts.
- Analyzes per-turn entry, minimum, and average speed.
- Compares brake point, peak pressure, duration, throttle commitment, gear, steering, and G-force.
- Measures ABS trigger count and duration and classifies excessive or fragmented intervention.
- Generates a responsive offline HTML report with desktop and mobile layouts.
- Uses one visual convention throughout: player as a red solid line, expert as a green dashed line.

## Skill Installation

Ask Codex to install the skill from:

```text
https://github.com/ikele123123/analyze-iracing-telemetry/tree/main/analyze-iracing-telemetry
```

Example prompt:

```text
Use $skill-installer to install the analyze-iracing-telemetry skill from
https://github.com/ikele123123/analyze-iracing-telemetry/tree/main/analyze-iracing-telemetry
```

After installation, start a new Codex conversation and invoke it with:

```text
Use $analyze-iracing-telemetry to compare my IBT and expert reference CSV and generate an interactive HTML report.
```

## Python Requirements

Python 3.10 or later is recommended.

```powershell
python -m pip install -r requirements.txt
```

## Direct Script Usage

Automatic discovery works when the work directory contains one `.ibt` and one `.csv` in the root or `references/` directory:

```powershell
python .\analyze-iracing-telemetry\scripts\analyze_telemetry.py `
  --workdir "E:\telemetry-session"
```

Explicit inputs:

```powershell
python .\analyze-iracing-telemetry\scripts\analyze_telemetry.py `
  --ibt "E:\telemetry-session\player.ibt" `
  --reference "E:\telemetry-session\references\expert.csv" `
  --output "E:\telemetry-session\telemetry_analysis_report.html"
```

Useful overrides:

```powershell
# Reference filename does not include its lap time
--reference-time "1:35.229"

# Override track metadata
--corner-count 14

# Override automatic steering-peak detection
--corner-centers "0.11,0.20,0.28"
```

## Reference CSV

The analyzer is designed for Garage 61-style exports containing at least:

```text
Speed, LapDistPct, Lat, Lon, Brake, Throttle, RPM,
SteeringWheelAngle, Gear, LatAccel, LongAccel, YawRate
```

`ABSActive` is optional. If absent, expert ABS activity is treated as unavailable/false.

## Turn Detection

Automatic turn detection is a heuristic based on expert steering peaks and the iRacing `TrackNumTurns` value. Always inspect T-number placement on a new track. Use `--corner-centers` when official numbering differs, a long corner produces multiple peaks, or a light bend is omitted.

## Privacy

Telemetry remains local. The generated report embeds processed data but does not upload files. Do not commit personal `.ibt`, reference `.csv`, generated `.html`, screenshots, credentials, or private setup data.

## License

MIT
