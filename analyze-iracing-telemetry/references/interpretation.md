# Telemetry Interpretation Reference

## Analysis Order

1. Confirm that the chosen player lap is complete, off-pit, and free of off-track samples.
2. Confirm player and expert lap times before comparing controls.
3. Validate turn centers and windows on the GPS map.
4. Rank corners by time loss, then diagnose line, speed, brake/ABS, throttle, gear, and steering in that order.
5. Separate measured facts from coaching in the final summary.

## Metric Meaning

- **Entry speed**: speed at the start of the turn window. Interpret only with brake point and line.
- **Minimum speed**: lowest speed in the window. A higher minimum is not automatically better if the line is longer or throttle is delayed.
- **Average speed**: window distance divided by window elapsed time. This is often more useful than minimum speed.
- **Brake-point delta**: player point minus expert point in meters. Positive means later; later is not automatically better.
- **Throttle-point delta**: player full-throttle point minus expert point. Positive means later.
- **Coast duration**: time with throttle below 10% and brake below 5%. Excess coast often signals an undecided transition.
- **Line RMS**: root-mean-square lateral displacement from the expert GPS line at matched lap progress. Use as a trend because GPS and distance alignment introduce meter-scale error.
- **Corner time delta**: player window duration minus expert window duration. The sum of turn windows need not equal the full-lap gap because straights and uncovered transitions remain.

## ABS Interpretation

Use ABS as evidence about brake execution, not as a binary target.

- Brief ABS intervention near peak braking can be normal.
- Longer intervention than the expert suggests excessive pressure, a fast pedal ramp, or lower available grip.
- Many short on/off events suggest unstable pressure near the grip limit.
- No ABS does not prove braking is too weak; an early brake point can still produce low entry speed without ABS.
- Compare ABS duration, event count, peak brake, deceleration G, brake point, and minimum speed together.
- Do not recommend eliminating ABS when the expert also uses it for a similar or longer duration.
- Mention weather, tire state, fuel, brake bias, setup, and hardware calibration when those are unavailable.

Suggested classifications:

- `无制动`: peak brake below 5%.
- `介入偏多`: player ABS duration exceeds expert by more than about 0.12 s and is materially sustained.
- `触发偏碎`: player has at least three more trigger segments with meaningful total duration.
- `介入可控`: ABS appears but is not materially longer or more fragmented than the expert.
- `未触发`: no meaningful player ABS sample; do not infer adequacy from this alone.

## Coaching Rules

- Later braking plus a lower minimum speed usually means "late, hard, and over-slowed"; recommend moving initial braking earlier and releasing sooner.
- Earlier braking with excess coast suggests moving the brake point later or carrying light brake pressure to the turn-in transition.
- A late full-throttle point with extra steering suggests fixing line and steering unwind before demanding earlier full throttle.
- Extra steering with lower average speed suggests tire scrub or insufficient track width usage.
- A gear mismatch matters when it changes acceleration, balance, or forces a shift at peak steering; do not flag neutral samples during a shift as an intended gear.
- Prioritize the largest three or four time-loss corners. Avoid overwhelming the driver with equal-priority advice for every turn.

## Limitations To State

- Garage 61-style CSV files may omit fuel, setup, tire state, and weather.
- LapDistPct alignment compares equal nominal progress, not an exact shared physical timestamp.
- GPS line differences can combine lateral offset and small longitudinal alignment error.
- Automatically detected steering peaks may not match official turn numbering.
- IBT best-lap channels can update after the lap boundary; validate the selected lap rather than choosing the first equal-duration segment.
