from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path

import irsdk
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yaml
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks, peak_prominences


IBT_PATH: Path
REF_PATH: Path
OUTPUT_PATH: Path
REFERENCE_TIME_OVERRIDE: float | None = None
CORNER_COUNT_OVERRIDE: int | None = None

USER_COLOR = "#d83a2e"
REF_COLOR = "#15803d"
INK = "#17201c"
MUTED = "#66736d"
GRID_COLOR = "#dfe4df"
PAPER = "#ffffff"

CORNER_CENTERS: list[float] = []
CORNER_BOUNDS: list[tuple[float, float]] = []


def parse_lap_time_from_name(path: Path) -> float:
    if REFERENCE_TIME_OVERRIDE is not None:
        return REFERENCE_TIME_OVERRIDE
    match = re.search(r"(\d{1,2})\.(\d{2})\.(\d{3})", path.name)
    if not match:
        raise ValueError(
            f"Cannot parse reference lap time from {path.name}. "
            "Pass --reference-time as seconds or M:SS.mmm."
        )
    minutes, seconds, millis = map(int, match.groups())
    return minutes * 60 + seconds + millis / 1000


def parse_time_value(value: str) -> float:
    value = value.strip()
    if ":" in value:
        minutes, seconds = value.split(":", 1)
        return float(minutes) * 60 + float(seconds)
    return float(value)


def detect_corner_centers(ref_df: pd.DataFrame, corner_count: int) -> list[float]:
    if corner_count < 1:
        raise ValueError("Corner count must be positive")
    progress = np.linspace(0, 1, 6000, endpoint=False)
    steer = periodic_interp(
        progress,
        ref_df["LapDistPct"].to_numpy(),
        np.abs(np.rad2deg(ref_df["SteeringWheelAngle"].to_numpy())),
    )
    speed = periodic_interp(progress, ref_df["LapDistPct"].to_numpy(), ref_df["Speed"].to_numpy()) * 3.6
    smooth = gaussian_filter1d(steer, 12)
    minimum_spacing = max(24, int(len(progress) / (corner_count * 5.5)))
    peaks, _ = find_peaks(smooth, prominence=1.2, distance=minimum_spacing)
    if len(peaks) < corner_count:
        peaks, _ = find_peaks(smooth, prominence=0.35, distance=max(12, minimum_spacing // 2))
    if len(peaks) < corner_count:
        raise RuntimeError(
            f"Only detected {len(peaks)} steering peaks for {corner_count} turns. "
            "Pass --corner-centers with comma-separated LapDistPct values."
        )
    prominence = peak_prominences(smooth, peaks)[0]
    speed_deficit = np.max(speed) - speed[peaks]
    score = prominence * 1.8 + smooth[peaks] * 0.35 + speed_deficit * 0.08
    selected = peaks[np.argsort(score)[-corner_count:]]
    return [float(progress[index]) for index in np.sort(selected)]


def make_corner_bounds(centers: list[float]) -> list[tuple[float, float]]:
    ordered = sorted(centers)
    result = []
    for index, center in enumerate(ordered):
        previous = ordered[index - 1] if index else ordered[-1] - 1
        following = ordered[index + 1] if index + 1 < len(ordered) else ordered[0] + 1
        approach = min(0.045, max(0.015, (center - previous) * 0.43))
        exit_distance = min(0.045, max(0.015, (following - center) * 0.43))
        result.append((max(0.0, center - approach), min(1.0, center + exit_distance)))
    return result


def fmt_time(seconds: float, signed: bool = False) -> str:
    if signed:
        return f"{seconds:+.3f} s"
    minutes = int(seconds // 60)
    return f"{minutes}:{seconds - minutes * 60:06.3f}"


def finite(value: float, digits: int = 3) -> float:
    return round(float(value), digits) if np.isfinite(value) else 0.0


def periodic_interp(target: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    order = np.argsort(x)
    x_sorted = np.asarray(x)[order]
    y_sorted = np.asarray(y)[order]
    keep = np.r_[True, np.diff(x_sorted) > 1e-8]
    x_sorted, y_sorted = x_sorted[keep], y_sorted[keep]
    return np.interp(
        target,
        np.r_[x_sorted - 1, x_sorted, x_sorted + 1],
        np.r_[y_sorted, y_sorted, y_sorted],
    )


def load_ibt() -> tuple[pd.DataFrame, dict, float, int]:
    ibt = irsdk.IBT()
    ibt.open(str(IBT_PATH))
    try:
        raw_info = ibt._shared_mem[
            ibt._header.session_info_offset:
            ibt._header.session_info_offset + ibt._header.session_info_len
        ].rstrip(b"\0").decode("latin1")
        session_info = yaml.safe_load(raw_info)
        channels = [
            "SessionTime", "Lap", "LapDistPct", "Speed", "Throttle", "Brake",
            "Clutch", "Gear", "RPM", "SteeringWheelAngle", "Lat", "Lon",
            "Alt", "Yaw", "YawRate", "LatAccel", "LongAccel", "OnPitRoad",
            "PlayerTrackSurface", "PlayerCarMyIncidentCount", "LapBestLapTime",
            "BrakeABSactive",
        ]
        data = {name: ibt.get_all(name) for name in channels if name in ibt.var_headers_names}
        df = pd.DataFrame(data)
        tick_rate = int(ibt._header.tick_rate)
    finally:
        ibt.close()

    valid_best_times = df.loc[df["LapBestLapTime"] > 0, "LapBestLapTime"]
    recorded_best = float(valid_best_times.min()) if not valid_best_times.empty else -1.0
    clean_candidates = []
    for lap_no, lap_df in df.groupby("Lap"):
        if lap_no <= 0 or len(lap_df) < tick_rate * 60:
            continue
        complete = lap_df["LapDistPct"].min() < 0.01 and lap_df["LapDistPct"].max() > 0.99
        on_pit = bool(lap_df["OnPitRoad"].any())
        off_track = int((lap_df["PlayerTrackSurface"] != 3).sum())
        duration = float(lap_df["SessionTime"].iloc[-1] - lap_df["SessionTime"].iloc[0] + 1 / tick_rate)
        if complete and not on_pit and off_track == 0:
            clean_candidates.append((abs(duration - recorded_best), duration, int(lap_no)))

    if not clean_candidates:
        raise RuntimeError("No complete clean lap found in IBT")
    clean_laps = {lap_no: duration for _, duration, lap_no in clean_candidates}
    best_updates = df.loc[np.isclose(df["LapBestLapTime"], recorded_best, atol=1e-3), "Lap"]
    inferred_best_lap = int(best_updates.iloc[0]) - 1 if not best_updates.empty else -1
    if inferred_best_lap in clean_laps:
        best_lap_no = inferred_best_lap
        raw_duration = clean_laps[best_lap_no]
    else:
        _, raw_duration, best_lap_no = min(clean_candidates)
    best_time = recorded_best
    if best_time <= 0:
        best_time = raw_duration
    lap_df = df[df["Lap"] == best_lap_no].copy().sort_values("LapDistPct")
    return lap_df, session_info, best_time, tick_rate


def build_trace_data(
    lap_df: pd.DataFrame, ref_df: pd.DataFrame, user_time: float, ref_time: float,
    track_length_m: float,
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
    progress = np.linspace(0, 1, 1801)
    fields = [
        "Speed", "Throttle", "Brake", "Gear", "RPM", "SteeringWheelAngle",
        "Lat", "Lon", "LatAccel", "LongAccel", "YawRate",
    ]
    user = {
        field: periodic_interp(progress, lap_df["LapDistPct"].to_numpy(), lap_df[field].to_numpy())
        for field in fields
    }
    ref = {
        field: periodic_interp(progress, ref_df["LapDistPct"].to_numpy(), ref_df[field].to_numpy())
        for field in fields
    }
    user_abs = (
        lap_df["BrakeABSactive"].astype(str).str.lower().isin({"true", "1"}).astype(float).to_numpy()
        if "BrakeABSactive" in lap_df else np.zeros(len(lap_df))
    )
    ref_abs = (
        ref_df["ABSActive"].astype(str).str.lower().isin({"true", "1"}).astype(float).to_numpy()
        if "ABSActive" in ref_df else np.zeros(len(ref_df))
    )
    user["ABS"] = periodic_interp(progress, lap_df["LapDistPct"].to_numpy(), user_abs) >= 0.5
    ref["ABS"] = periodic_interp(progress, ref_df["LapDistPct"].to_numpy(), ref_abs) >= 0.5

    def cumulative_time(speed: np.ndarray, official_time: float) -> np.ndarray:
        ds = track_length_m * np.diff(progress)
        avg_speed = np.maximum((speed[1:] + speed[:-1]) / 2, 1)
        result = np.r_[0, np.cumsum(ds / avg_speed)]
        return result * (official_time / result[-1])

    user["Time"] = cumulative_time(user["Speed"], user_time)
    ref["Time"] = cumulative_time(ref["Speed"], ref_time)
    user["SpeedKph"] = user["Speed"] * 3.6
    ref["SpeedKph"] = ref["Speed"] * 3.6
    user["SteerDeg"] = np.rad2deg(user["SteeringWheelAngle"])
    ref["SteerDeg"] = np.rad2deg(ref["SteeringWheelAngle"])
    user["LatG"] = user["LatAccel"] / 9.80665
    ref["LatG"] = ref["LatAccel"] / 9.80665
    user["LongG"] = user["LongAccel"] / 9.80665
    ref["LongG"] = ref["LongAccel"] / 9.80665

    lat0 = float(np.mean(ref["Lat"]))
    lon0 = float(np.mean(ref["Lon"]))
    m_per_lat = 111_320.0
    m_per_lon = 111_320.0 * math.cos(math.radians(lat0))
    for trace in (user, ref):
        trace["X"] = (trace["Lon"] - lon0) * m_per_lon
        trace["Y"] = (trace["Lat"] - lat0) * m_per_lat

    tx = np.gradient(ref["X"])
    ty = np.gradient(ref["Y"])
    norm = np.hypot(tx, ty)
    nx, ny = -ty / norm, tx / norm
    user["LineDelta"] = (user["X"] - ref["X"]) * nx + (user["Y"] - ref["Y"]) * ny
    user["LineDistance"] = np.hypot(user["X"] - ref["X"], user["Y"] - ref["Y"])
    return progress, user, ref


def first_sustained(indices: np.ndarray, condition: np.ndarray, count: int = 4) -> int | None:
    if len(indices) < count:
        return None
    hits = condition[indices].astype(int)
    run = np.convolve(hits, np.ones(count, dtype=int), mode="valid")
    found = np.flatnonzero(run == count)
    return int(indices[found[0]]) if len(found) else None


def turn_metrics(
    number: int, start: float, end: float, center: float, progress: np.ndarray,
    user: dict[str, np.ndarray], ref: dict[str, np.ndarray], track_length_m: float,
) -> dict:
    zone = np.flatnonzero((progress >= start) & (progress <= end))
    pre_apex = zone[progress[zone] <= center + 0.02]
    result: dict[str, object] = {
        "number": number,
        "name": f"T{number}",
        "start": start,
        "end": end,
        "center": center,
        "start_m": start * track_length_m,
        "end_m": end * track_length_m,
    }

    apex_indices = {}
    brake_indices = {}
    throttle_indices = {}
    for label, trace in (("user", user), ("ref", ref)):
        apex = int(zone[np.argmin(trace["SpeedKph"][zone])])
        apex_indices[label] = apex
        brake = first_sustained(pre_apex, trace["Brake"] > 0.05, 3)
        post_apex = zone[zone >= apex]
        throttle = first_sustained(post_apex, trace["Throttle"] >= 0.95, 5)
        brake_indices[label] = brake
        throttle_indices[label] = throttle
        dt = np.gradient(trace["Time"][zone])
        zone_time = trace["Time"][zone[-1]] - trace["Time"][zone[0]]
        abs_state = trace["ABS"][zone].astype(bool)
        abs_duration = float(np.sum(dt[abs_state]))
        abs_events = int(np.sum(np.diff(abs_state.astype(int), prepend=0) == 1))
        abs_hits = zone[abs_state]
        result[label] = {
            "entry_speed": float(trace["SpeedKph"][zone[0]]),
            "min_speed": float(trace["SpeedKph"][apex]),
            "avg_speed": float((end - start) * track_length_m / max(zone_time, 0.01) * 3.6),
            "apex_m": float(progress[apex] * track_length_m),
            "brake_point_m": None if brake is None else float(progress[brake] * track_length_m),
            "brake_peak": float(np.max(trace["Brake"][zone]) * 100),
            "brake_duration": float(np.sum(dt[trace["Brake"][zone] > 0.05])),
            "abs_events": abs_events,
            "abs_duration": abs_duration,
            "abs_first_m": None if not len(abs_hits) else float(progress[abs_hits[0]] * track_length_m),
            "throttle_point_m": None if throttle is None else float(progress[throttle] * track_length_m),
            "throttle_min": float(np.min(trace["Throttle"][zone]) * 100),
            "full_throttle_pct": float(np.sum(dt[trace["Throttle"][zone] >= 0.95]) / max(zone_time, 0.01) * 100),
            "coast_duration": float(np.sum(dt[(trace["Throttle"][zone] < 0.1) & (trace["Brake"][zone] < 0.05)])),
            "apex_gear": int(round(trace["Gear"][apex])),
            "min_gear": int(round(np.min(trace["Gear"][zone]))),
            "steer_peak": float(np.max(np.abs(trace["SteerDeg"][zone]))),
            "lat_g_peak": float(np.max(np.abs(trace["LatG"][zone]))),
            "brake_g_peak": float(max(0, -np.min(trace["LongG"][zone]))),
            "time": float(zone_time),
        }

    result["delta_time"] = float(result["user"]["time"] - result["ref"]["time"])
    result["delta_entry"] = float(result["user"]["entry_speed"] - result["ref"]["entry_speed"])
    result["delta_min"] = float(result["user"]["min_speed"] - result["ref"]["min_speed"])
    result["delta_avg"] = float(result["user"]["avg_speed"] - result["ref"]["avg_speed"])
    result["line_rms"] = float(np.sqrt(np.mean(user["LineDelta"][zone] ** 2)))
    result["line_peak"] = float(np.max(np.abs(user["LineDelta"][zone])))
    result["apex_line"] = float(user["LineDelta"][apex_indices["ref"]])

    ub, rb = brake_indices["user"], brake_indices["ref"]
    ut, rt = throttle_indices["user"], throttle_indices["ref"]
    result["brake_delta_m"] = None if ub is None or rb is None else float((progress[ub] - progress[rb]) * track_length_m)
    result["throttle_delta_m"] = None if ut is None or rt is None else float((progress[ut] - progress[rt]) * track_length_m)

    u_abs, r_abs = result["user"], result["ref"]
    if u_abs["brake_peak"] < 5:
        result["abs_assessment"] = "无制动"
    elif u_abs["abs_duration"] > r_abs["abs_duration"] + 0.12 and u_abs["abs_duration"] > 0.15:
        result["abs_assessment"] = "介入偏多"
    elif u_abs["abs_events"] > r_abs["abs_events"] + 2 and u_abs["abs_duration"] > 0.10:
        result["abs_assessment"] = "触发偏碎"
    elif u_abs["abs_duration"] > 0.02:
        result["abs_assessment"] = "介入可控"
    else:
        result["abs_assessment"] = "未触发"

    steer_at_center = ref["SteerDeg"][int(np.argmin(np.abs(progress - center)))]
    result["direction"] = "左" if steer_at_center > 0 else "右"
    result["recommendations"] = recommendations(result)
    return result


def recommendations(turn: dict) -> list[str]:
    u, r = turn["user"], turn["ref"]
    recs: list[str] = []
    brake_delta = turn["brake_delta_m"]
    throttle_delta = turn["throttle_delta_m"]
    if turn["delta_min"] < -2.0:
        recs.append(f"最低速少 {abs(turn['delta_min']):.1f} km/h：减少入弯后的额外减速，目标最低速约 {r['min_speed']:.0f} km/h。")
    elif turn["delta_min"] > 3.0 and turn["delta_time"] > 0.08:
        recs.append("最低速并不低但仍丢时间，重点检查线路长度和出弯油门，而非继续提高入弯速度。")
    if brake_delta is not None:
        if brake_delta > 8:
            recs.append(f"刹车比参考晚约 {brake_delta:.0f} m；若伴随更低最低速，前移刹车点并更早释放，避免“晚刹、深踩、过慢”。")
        elif brake_delta < -8:
            recs.append(f"刹车比参考早约 {abs(brake_delta):.0f} m；保持初段制动力稳定，逐步把起刹点后移。")
    if u["brake_peak"] > r["brake_peak"] + 12:
        recs.append(f"峰值刹车高 {u['brake_peak'] - r['brake_peak']:.0f} 个百分点，优先缩短重刹阶段并平顺卸载前轴。")
    if u["abs_duration"] > r["abs_duration"] + 0.12:
        recs.append(f"ABS 累计介入 {u['abs_duration']:.2f} s，参考为 {r['abs_duration']:.2f} s；降低峰值或放缓踩下速率，避免持续依赖 ABS。")
    elif u["abs_events"] > r["abs_events"] + 2:
        recs.append(f"ABS 反复触发 {u['abs_events']} 次，参考为 {r['abs_events']} 次；稳定踏板压力，减少抓地极限附近的反复加压。")
    if throttle_delta is not None and throttle_delta > 8:
        recs.append(f"全油门比参考晚约 {throttle_delta:.0f} m；在方向盘开始回正时更早渐进补油。")
    if u["coast_duration"] > r["coast_duration"] + 0.15:
        recs.append(f"无刹无油多 {u['coast_duration'] - r['coast_duration']:.2f} s；把滑行段改为轻刹带入或更早维持油门。")
    if u["steer_peak"] > r["steer_peak"] + 8:
        recs.append(f"方向盘峰值多 {u['steer_peak'] - r['steer_peak']:.0f}°，说明线路/车速造成额外转向；更充分使用外侧—顶点—外侧。")
    if u["apex_gear"] != r["apex_gear"]:
        recs.append(f"顶点使用 {u['apex_gear']} 挡，参考为 {r['apex_gear']} 挡；核对降挡时机并避免在最大转向时换挡。")
    if turn["line_rms"] > 1.2:
        side = "参考线左侧" if turn["apex_line"] > 0 else "参考线右侧"
        recs.append(f"线路均方偏差 {turn['line_rms']:.1f} m，顶点附近位于{side}约 {abs(turn['apex_line']):.1f} m；以参考 GPS 叠线为目标修正。")
    if not recs:
        recs.append("该弯动作已接近参考；保持节奏，优先把注意力放到更高损失弯。")
    return recs[:4]


def base_layout(fig: go.Figure, height: int, title: str = "") -> None:
    fig.update_layout(
        title={"text": title, "font": {"size": 18, "color": INK}},
        height=height, paper_bgcolor=PAPER, plot_bgcolor=PAPER,
        font={"family": "Segoe UI, Microsoft YaHei, sans-serif", "color": INK, "size": 12},
        margin={"l": 58, "r": 28, "t": 55 if title else 24, "b": 45},
        hovermode="x unified", legend={"orientation": "h", "y": 1.06, "x": 0},
    )
    fig.update_xaxes(showgrid=True, gridcolor=GRID_COLOR, zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor=GRID_COLOR, zeroline=False)


def build_figures(progress: np.ndarray, user: dict, ref: dict, turns: list[dict]) -> tuple[str, str]:
    map_fig = go.Figure()
    map_fig.add_trace(go.Scattergl(
        x=ref["X"], y=ref["Y"], mode="lines", name="高手线路",
        line={"color": REF_COLOR, "width": 3, "dash": "dash"}, hovertemplate="高手<br>%{text}<extra></extra>",
        text=[f"赛道 {p*100:.1f}%" for p in progress],
    ))
    map_fig.add_trace(go.Scattergl(
        x=user["X"], y=user["Y"], mode="lines", name="我的线路",
        line={"color": USER_COLOR, "width": 2.5}, hovertemplate="我的线路<br>%{text}<extra></extra>",
        text=[f"赛道 {p*100:.1f}%" for p in progress],
    ))
    base_layout(map_fig, 650, "真实 GPS 线路叠加")
    map_fig.update_layout(hovermode="closest", dragmode="pan")
    map_fig.update_xaxes(title="东向距离 (m)", scaleanchor="y", scaleratio=1)
    map_fig.update_yaxes(title="北向距离 (m)")
    tx, ty = np.gradient(ref["X"]), np.gradient(ref["Y"])
    for i, center in enumerate(CORNER_CENTERS, start=1):
        idx = int(np.argmin(np.abs(progress - center)))
        length = max(float(np.hypot(tx[idx], ty[idx])), 1e-6)
        nx, ny = -ty[idx] / length, tx[idx] / length
        side = -1 if i in {3, 5, 8, 11, 13} else 1
        map_fig.add_annotation(
            x=ref["X"][idx], y=ref["Y"][idx], text=f"T{i}", showarrow=True,
            ax=side * nx * 38, ay=-side * ny * 38, arrowhead=2, arrowsize=0.8,
            arrowwidth=1, arrowcolor=INK, bgcolor="#ffffff", bordercolor=INK,
            borderwidth=1, borderpad=3, font={"size": 10, "color": INK},
        )

    loss_fig = go.Figure()
    names = [t["name"] for t in turns]
    losses = [t["delta_time"] for t in turns]
    loss_fig.add_trace(go.Bar(
        x=names, y=losses, name="区段时间差",
        marker={"color": [USER_COLOR if v >= 0 else REF_COLOR for v in losses]},
        text=[f"{v:+.2f}" for v in losses], textposition="outside",
        hovertemplate="%{x}<br>时间差 %{y:+.3f} s<extra></extra>",
    ))
    loss_fig.add_hline(y=0, line_color=INK, line_width=1)
    base_layout(loss_fig, 390, "各弯区段时间损失（含入弯与出弯）")
    loss_fig.update_yaxes(title="我的时间 - 高手时间 (s)")
    loss_fig.update_xaxes(title="弯道")

    config = {"displaylogo": False, "responsive": True, "scrollZoom": True, "locale": "zh-CN"}
    return (
        map_fig.to_html(full_html=False, include_plotlyjs="inline", config=config),
        loss_fig.to_html(full_html=False, include_plotlyjs=False, config=config),
    )


def turn_table(turns: list[dict]) -> str:
    rows = []
    for t in turns:
        u, r = t["user"], t["ref"]
        cls = "loss" if t["delta_time"] > 0.08 else "gain" if t["delta_time"] < -0.04 else "even"
        bp = "—" if t["brake_delta_m"] is None else f"{t['brake_delta_m']:+.0f} m"
        tp = "—" if t["throttle_delta_m"] is None else f"{t['throttle_delta_m']:+.0f} m"
        rows.append(f"""
        <tr data-turn="{t['number']}">
          <td><button class="turn-link" data-turn="{t['number']}">{t['name']}</button><span class="dir">{t['direction']}</span></td>
          <td class="{cls}">{t['delta_time']:+.3f}</td>
          <td>{u['entry_speed']:.0f} / {r['entry_speed']:.0f}</td>
          <td>{u['min_speed']:.0f} / {r['min_speed']:.0f}</td>
          <td>{u['avg_speed']:.0f} / {r['avg_speed']:.0f}</td>
          <td>{bp}</td><td>{u['brake_peak']:.0f} / {r['brake_peak']:.0f}%</td>
          <td>{t['abs_assessment']} · {u['abs_events']}次 {u['abs_duration']:.2f}s / {r['abs_events']}次 {r['abs_duration']:.2f}s</td>
          <td>{tp}</td><td>{u['apex_gear']} / {r['apex_gear']}</td>
          <td>{u['steer_peak']:.0f} / {r['steer_peak']:.0f}°</td><td>{t['line_rms']:.1f} m</td>
        </tr>""")
    return "\n".join(rows)


def detail_payload(progress: np.ndarray, user: dict, ref: dict, turns: list[dict], track_length_m: float) -> list[dict]:
    payload = []
    for t in turns:
        pad = 0.008
        zone = np.flatnonzero((progress >= t["start"] - pad) & (progress <= t["end"] + pad))
        stride = max(1, len(zone) // 240)
        idx = zone[::stride]
        u, r = t["user"], t["ref"]
        payload.append({
            "number": t["number"], "name": t["name"], "direction": t["direction"],
            "range": [finite(t["start"] * track_length_m, 1), finite(t["end"] * track_length_m, 1)],
            "distance": [finite(v * track_length_m, 1) for v in progress[idx]],
            "ux": [finite(v, 2) for v in user["X"][idx]], "uy": [finite(v, 2) for v in user["Y"][idx]],
            "rx": [finite(v, 2) for v in ref["X"][idx]], "ry": [finite(v, 2) for v in ref["Y"][idx]],
            "userSpeed": [finite(v, 1) for v in user["SpeedKph"][idx]],
            "refSpeed": [finite(v, 1) for v in ref["SpeedKph"][idx]],
            "userBrake": [finite(v * 100, 1) for v in user["Brake"][idx]],
            "refBrake": [finite(v * 100, 1) for v in ref["Brake"][idx]],
            "userThrottle": [finite(v * 100, 1) for v in user["Throttle"][idx]],
            "refThrottle": [finite(v * 100, 1) for v in ref["Throttle"][idx]],
            "userSteer": [finite(v, 1) for v in user["SteerDeg"][idx]],
            "refSteer": [finite(v, 1) for v in ref["SteerDeg"][idx]],
            "userGear": [int(round(v)) for v in user["Gear"][idx]],
            "refGear": [int(round(v)) for v in ref["Gear"][idx]],
            "userABS": [100 if v else 0 for v in user["ABS"][idx]],
            "refABS": [100 if v else 0 for v in ref["ABS"][idx]],
            "metrics": {
                "time": f"{t['delta_time']:+.3f} s",
                "entry": f"{u['entry_speed']:.0f} / {r['entry_speed']:.0f} km/h",
                "minimum": f"{u['min_speed']:.0f} / {r['min_speed']:.0f} km/h",
                "average": f"{u['avg_speed']:.0f} / {r['avg_speed']:.0f} km/h",
                "brake": f"{u['brake_peak']:.0f} / {r['brake_peak']:.0f}% · {u['brake_duration']:.2f} / {r['brake_duration']:.2f} s",
                "abs": f"{t['abs_assessment']} · {u['abs_events']}次 {u['abs_duration']:.2f}s / {r['abs_events']}次 {r['abs_duration']:.2f}s",
                "throttle": f"{u['full_throttle_pct']:.0f} / {r['full_throttle_pct']:.0f}% 区段时间",
                "gear": f"{u['apex_gear']} / {r['apex_gear']} 挡",
                "steer": f"{u['steer_peak']:.0f} / {r['steer_peak']:.0f}°",
                "line": f"RMS {t['line_rms']:.1f} m · 峰值 {t['line_peak']:.1f} m",
                "gforce": f"侧向 {u['lat_g_peak']:.2f} / {r['lat_g_peak']:.2f} G · 减速 {u['brake_g_peak']:.2f} / {r['brake_g_peak']:.2f} G",
            },
            "recommendations": t["recommendations"],
        })
    return payload


def make_report() -> None:
    global CORNER_CENTERS, CORNER_BOUNDS
    lap_df, info, user_time, tick_rate = load_ibt()
    ref_df = pd.read_csv(REF_PATH, encoding="utf-8")
    required_reference = {
        "Speed", "LapDistPct", "Lat", "Lon", "Brake", "Throttle", "RPM",
        "SteeringWheelAngle", "Gear", "LatAccel", "LongAccel", "YawRate",
    }
    missing_reference = sorted(required_reference - set(ref_df.columns))
    if missing_reference:
        raise ValueError(f"Reference CSV is missing required columns: {', '.join(missing_reference)}")
    ref_time = parse_lap_time_from_name(REF_PATH)
    weekend = info["WeekendInfo"]
    track_length_m = float(str(weekend["TrackLength"]).split()[0]) * 1000
    if not CORNER_CENTERS:
        corner_count = CORNER_COUNT_OVERRIDE or int(weekend.get("TrackNumTurns", 0))
        if corner_count < 1:
            raise ValueError("IBT does not provide TrackNumTurns; pass --corner-count")
        CORNER_CENTERS = detect_corner_centers(ref_df, corner_count)
    CORNER_CENTERS = sorted(CORNER_CENTERS)
    CORNER_BOUNDS = make_corner_bounds(CORNER_CENTERS)
    driver_idx = info["DriverInfo"]["DriverCarIdx"]
    driver = next(
        (item for item in info["DriverInfo"]["Drivers"] if item.get("CarIdx") == driver_idx),
        info["DriverInfo"]["Drivers"][0],
    )
    progress, user, ref = build_trace_data(lap_df, ref_df, user_time, ref_time, track_length_m)
    turns = [
        turn_metrics(i, bounds[0], bounds[1], center, progress, user, ref, track_length_m)
        for i, (bounds, center) in enumerate(zip(CORNER_BOUNDS, CORNER_CENTERS), start=1)
    ]
    map_html, loss_html = build_figures(progress, user, ref, turns)
    payload = detail_payload(progress, user, ref, turns, track_length_m)

    priority = sorted([t for t in turns if t["delta_time"] > 0], key=lambda t: t["delta_time"], reverse=True)[:4]
    priority_html = "".join(
        f"<li><strong>{t['name']}（{t['direction']}） · {t['delta_time']:+.3f} s</strong><span>{html.escape(t['recommendations'][0])}</span></li>"
        for t in priority
    )
    analyzed_loss = sum(t["delta_time"] for t in turns)
    speed_gap_min = np.min(user["SpeedKph"] - ref["SpeedKph"])
    metadata = {
        "track": f"{weekend['TrackDisplayName']} · {weekend['TrackConfigName']}",
        "car": driver.get("CarScreenName", "未知车辆"),
        "driver": driver.get("UserName", "Driver"),
        "surface": weekend.get("TrackSurfaceTemp", "未知"),
        "air": weekend.get("TrackAirTemp", "未知"),
    }

    css = r"""
    :root { --ink:#17201c; --muted:#66736d; --paper:#fff; --bg:#f2f4f1; --line:#dce2dc; --user:#d83a2e; --ref:#15803d; --warn:#f4c95d; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:"Segoe UI","Microsoft YaHei",sans-serif; line-height:1.5; letter-spacing:0; }
    header { background:#18231e; color:#fff; padding:34px clamp(20px,5vw,72px) 28px; border-bottom:5px solid var(--warn); }
    header .eyebrow { color:#a9bbb2; font-size:13px; text-transform:uppercase; }
    h1 { margin:7px 0 8px; font-size:clamp(28px,4vw,48px); line-height:1.12; font-weight:720; }
    header p { margin:0; color:#ced8d2; max-width:880px; }
    main { max-width:1500px; margin:0 auto; padding:24px clamp(14px,3vw,40px) 64px; }
    section { margin:0 0 24px; background:var(--paper); border:1px solid var(--line); border-radius:6px; overflow:hidden; }
    .section-head { padding:19px 22px 12px; border-bottom:1px solid var(--line); }
    h2 { margin:0 0 4px; font-size:22px; }
    h3 { margin:0; font-size:17px; }
    .sub { margin:0; color:var(--muted); font-size:13px; }
    .kpis { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:6px; overflow:hidden; margin-bottom:24px; }
    .kpi { background:#fff; padding:18px 20px; min-width:0; }
    .kpi span { display:block; color:var(--muted); font-size:12px; }
    .kpi strong { display:block; margin-top:4px; font-size:25px; white-space:nowrap; }
    .kpi.user strong { color:var(--user); } .kpi.ref strong { color:var(--ref); }
    .overview { display:grid; grid-template-columns:1.1fr .9fr; }
    .overview > div { padding:22px; }
    .overview > div + div { border-left:1px solid var(--line); }
    .priority { list-style:none; padding:0; margin:13px 0 0; }
    .priority li { display:grid; grid-template-columns:190px 1fr; gap:16px; padding:11px 0; border-top:1px solid var(--line); }
    .priority span { color:#44514b; }
    .legend { display:flex; gap:18px; flex-wrap:wrap; font-size:13px; color:var(--muted); }
    .sample { display:inline-block; width:25px; margin:0 7px 3px 0; border-top:3px solid var(--user); vertical-align:middle; }
    .sample.ref { border-top-color:var(--ref); border-top-style:dashed; }
    .plot-wrap { padding:8px 12px 12px; }
    .table-wrap { overflow:auto; }
    table { border-collapse:collapse; width:100%; min-width:1130px; font-size:12px; }
    th { position:sticky; top:0; background:#eef1ed; color:#52605a; text-align:right; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
    td { text-align:right; padding:9px 8px; border-bottom:1px solid #e8ece8; white-space:nowrap; }
    th:first-child, td:first-child { text-align:left; padding-left:18px; }
    tbody tr:hover { background:#fff8f3; }
    .turn-link { border:0; background:none; color:var(--ink); font-weight:700; padding:0; cursor:pointer; text-decoration:underline; text-decoration-color:#a8b4ad; }
    .dir { color:var(--muted); margin-left:6px; }
    td.loss { color:#b83720; font-weight:700; } td.gain { color:var(--ref); font-weight:700; } td.even { color:var(--muted); }
    .detail-tools { display:flex; flex-wrap:wrap; gap:7px; padding:14px 20px; border-bottom:1px solid var(--line); }
    .turn-button { width:42px; height:34px; border:1px solid #bfc8c1; border-radius:4px; background:#fff; color:var(--ink); cursor:pointer; font-weight:650; }
    .turn-button.active { color:#fff; background:var(--ink); border-color:var(--ink); }
    .detail-title { display:flex; justify-content:space-between; align-items:baseline; gap:12px; padding:18px 20px 0; }
    .detail-title span { color:var(--muted); }
    .detail-grid { display:grid; grid-template-columns:minmax(320px,.8fr) minmax(440px,1.2fr); gap:0; }
    .detail-grid > div { min-width:0; padding:8px 14px 12px; }
    #cornerMap { height:440px; } #cornerTelemetry { height:680px; position:relative; }
    #cornerTelemetry .hoverlayer .hovertext, #cornerTelemetry .hoverlayer .axistext { display:none !important; }
    .synced-tooltip-layer { position:absolute; inset:0; z-index:20; pointer-events:none; }
    .synced-hover-line { position:absolute; width:0; border-left:1px dashed #66736d; opacity:.72; }
    .synced-tooltip { position:absolute; padding:6px 8px; background:rgba(255,255,255,.96); border:1px solid #aeb8b1; border-radius:4px; box-shadow:0 2px 7px rgba(23,32,28,.14); font-size:10px; line-height:1.35; }
    .synced-tooltip strong { display:block; margin-bottom:2px; color:var(--ink); }
    .synced-tooltip span { display:block; white-space:nowrap; }
    .synced-tooltip .user-value { color:var(--user); }
    .synced-tooltip .ref-value { color:var(--ref); }
    .metric-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
    .metric { padding:13px 15px; border-right:1px solid var(--line); min-width:0; }
    .metric:nth-child(4n) { border-right:0; }
    .metric:nth-child(n+5) { border-top:1px solid var(--line); }
    .metric label { display:block; color:var(--muted); font-size:11px; }
    .metric strong { display:block; margin-top:3px; font-size:14px; overflow-wrap:anywhere; }
    .advice { padding:18px 22px 22px; background:#fbfcfa; }
    .advice ol { margin:10px 0 0; padding-left:24px; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 28px; }
    .method { padding:20px 22px; display:grid; grid-template-columns:repeat(3,1fr); gap:22px; }
    .method p { margin:5px 0 0; color:#52605a; font-size:13px; }
    footer { color:var(--muted); font-size:12px; padding:8px 2px; }
    @media(max-width:950px) { .kpis{grid-template-columns:repeat(2,1fr)} .overview,.detail-grid{grid-template-columns:1fr} .overview>div+div{border-left:0;border-top:1px solid var(--line)} .metric-grid{grid-template-columns:repeat(2,1fr)} .metric:nth-child(n){border:0;border-top:1px solid var(--line)} .advice ol{grid-template-columns:1fr} .method{grid-template-columns:1fr} }
    @media(max-width:560px) { main{padding-left:9px;padding-right:9px} .kpis{grid-template-columns:1fr 1fr} .kpi{padding:13px}.kpi strong{font-size:19px}.priority li{grid-template-columns:1fr;gap:3px} #cornerMap{height:340px} #cornerTelemetry{height:620px} }
    @media print { body{background:#fff} section{break-inside:avoid} .turn-button{display:none} }
    """

    table = turn_table(turns)
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    report = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iRacing 遥测对比 · {html.escape(metadata['track'])} · {html.escape(metadata['car'])}</title><style>{css}</style></head><body>
<header><div class="eyebrow">IRACING TELEMETRY DEBRIEF · GPS / INPUT / TIME ALIGNMENT</div>
<h1>{html.escape(metadata['track'])}</h1><p>{html.escape(metadata['driver'])} · {html.escape(metadata['car'])} · 个人最佳圈 vs Garage 61 高手参考圈</p></header>
<main>
<div class="kpis">
  <div class="kpi user"><span>我的最佳有效圈</span><strong>{fmt_time(user_time)}</strong></div>
  <div class="kpi ref"><span>高手参考圈</span><strong>{fmt_time(ref_time)}</strong></div>
  <div class="kpi"><span>整圈差距</span><strong style="color:var(--user)">+{user_time-ref_time:.3f} s</strong></div>
  <div class="kpi"><span>{len(turns)} 弯区段净差</span><strong>{analyzed_loss:+.3f} s</strong></div>
  <div class="kpi"><span>最大瞬时速度差</span><strong>{speed_gap_min:.1f} km/h</strong></div>
</div>
<section><div class="overview"><div><h2>首要改进顺序</h2><p class="sub">按弯道区段时间损失排序；先改善前四项，比零散追求每个刹车点更有效。</p><ol class="priority">{priority_html}</ol></div>
<div><h2>阅读基准</h2><p class="sub">所有曲线均按相同 LapDistPct 对齐：红色实线是你，绿色虚线是高手。正时间差表示你落后。</p>
<p><strong>对比环境：</strong>你的赛道温度 {metadata['surface']}，气温 {metadata['air']}。参考 CSV 不含天气与载油，绝对刹车距离需留出环境余量。</p>
<div class="legend"><span><i class="sample"></i>我的最佳圈</span><span><i class="sample ref"></i>高手 {fmt_time(ref_time)}</span></div></div></div></section>
<section><div class="section-head"><h2>赛道与线路</h2><p class="sub">真实经纬度投影；拖动平移、滚轮缩放，悬停查看赛道进度。线路偏差应结合方向盘和速度判断。</p></div><div class="plot-wrap">{map_html}</div></section>
<section><div class="section-head"><h2>弯道损失分布</h2><p class="sub">区段边界覆盖入弯、弯心和出弯；正值越大，训练优先级越高。</p></div><div class="plot-wrap">{loss_html}</div></section>
<section><div class="section-head"><h2>{len(turns)} 弯量化总表</h2><p class="sub">速度、刹车、ABS、档位和方向盘均为“我 / 高手”。刹车点与全油门点正值表示我更晚。</p></div><div class="table-wrap"><table><thead><tr>
<th>弯道</th><th>时间差 s</th><th>入弯 km/h</th><th>最低 km/h</th><th>平均 km/h</th><th>起刹点差</th><th>峰值刹车</th><th>ABS 次数/时长</th><th>全油门点差</th><th>顶点档位</th><th>方向盘峰值</th><th>线路 RMS</th>
</tr></thead><tbody>{table}</tbody></table></div></section>
<section id="detail"><div class="section-head"><h2>逐弯交互分析</h2><p class="sub">选择弯道后，局部线路与全部操作曲线同步切换。图表可缩放，双击恢复。</p></div>
<div class="detail-tools">{''.join(f'<button class="turn-button" data-turn="{i}">T{i}</button>' for i in range(1, len(turns) + 1))}</div>
<div class="detail-title"><h3 id="cornerTitle">T1</h3><span id="cornerRange"></span></div>
<div class="detail-grid"><div id="cornerMap"></div><div id="cornerTelemetry"></div></div>
<div class="metric-grid">
<div class="metric"><label>区段时间差</label><strong id="m-time"></strong></div><div class="metric"><label>入弯速度 我 / 高手</label><strong id="m-entry"></strong></div>
<div class="metric"><label>最低速度 我 / 高手</label><strong id="m-minimum"></strong></div><div class="metric"><label>平均速度 我 / 高手</label><strong id="m-average"></strong></div>
<div class="metric"><label>峰值刹车 · 持续时间</label><strong id="m-brake"></strong></div><div class="metric"><label>ABS 次数 · 累计时长</label><strong id="m-abs"></strong></div><div class="metric"><label>全油门时间占比</label><strong id="m-throttle"></strong></div>
<div class="metric"><label>顶点档位</label><strong id="m-gear"></strong></div><div class="metric"><label>方向盘峰值</label><strong id="m-steer"></strong></div>
<div class="metric"><label>线路偏差</label><strong id="m-line"></strong></div><div class="metric"><label>侧向 / 减速 G</label><strong id="m-gforce"></strong></div></div>
<div class="advice"><h3>该弯操作调整</h3><ol id="cornerAdvice"></ol></div></section>
<section><div class="section-head"><h2>方法与限制</h2></div><div class="method">
<div><h3>圈段选择</h3><p>从 IBT 自动排除进站、未完成与赛道表面异常圈，选择最接近记录最佳时间的完整干净圈。源数据 {tick_rate} Hz。</p></div>
<div><h3>距离与 ABS</h3><p>两圈重采样到统一赛道进度；GPS 差值投影到参考轨迹法线。ABS 按原始开关状态统计触发段数与累计介入时长。</p></div>
<div><h3>使用边界</h3><p>参考文件无载油、轮胎、天气和设置数据。线路 GPS 有采样误差；建议把米级差异当趋势，并在同环境下逐项验证。</p></div>
</div></section>
<footer>数据源：{html.escape(IBT_PATH.name)} · {html.escape(REF_PATH.name)} · 生成报告只分析本地文件，不上传遥测。</footer>
</main>
<script>
const turns={data_json}; const U='{USER_COLOR}', R='{REF_COLOR}', INK='{INK}', GRID='{GRID_COLOR}';
const cfg={{responsive:true,displaylogo:false,scrollZoom:true,locale:'zh-CN'}};
function line(x,y,name,color,dash='solid',shape='linear',showlegend=false){{return {{x,y,type:'scattergl',mode:'lines',name,showlegend,line:{{color,width:2,dash,shape}},hovertemplate:name+'<br>%{{x:.0f}} m · %{{y:.1f}}<extra></extra>'}};}}
function mapLine(x,y,distance,name,color,dash='solid'){{const trace=line(x,y,name,color,dash,'linear',true);trace.customdata=distance;trace.hovertemplate=name+'<br>%{{customdata:.0f}} m<extra></extra>';return trace;}}
function positionMarker(name,color,size,symbol){{return {{x:[null],y:[null],type:'scatter',mode:'markers',name,showlegend:false,hoverinfo:'skip',marker:{{size,color,symbol,line:{{color:symbol==='circle-open'?color:'#fff',width:2}}}}}};}}
function setupCornerHover(mapPlot,telemetryPlot,data){{
 const specs=[
  {{title:'速度',user:'userSpeed',ref:'refSpeed',unit:'km/h',digits:1,axis:'yaxis'}},
  {{title:'刹车',user:'userBrake',ref:'refBrake',unit:'%',digits:1,axis:'yaxis2'}},
  {{title:'油门',user:'userThrottle',ref:'refThrottle',unit:'%',digits:1,axis:'yaxis3'}},
  {{title:'方向盘',user:'userSteer',ref:'refSteer',unit:'°',digits:1,axis:'yaxis4'}},
  {{title:'档位',user:'userGear',ref:'refGear',unit:'挡',digits:0,axis:'yaxis5'}},
  {{title:'ABS',user:'userABS',ref:'refABS',unit:'',digits:0,axis:'yaxis6',binary:true}},
 ];
 [mapPlot,telemetryPlot].forEach(plot=>{{plot.removeAllListeners('plotly_hover');plot.removeAllListeners('plotly_unhover');}});
 if(mapPlot._cornerMarkerFrame)cancelAnimationFrame(mapPlot._cornerMarkerFrame);
 telemetryPlot.querySelector('.synced-tooltip-layer')?.remove();
 const layer=document.createElement('div'); layer.className='synced-tooltip-layer'; layer.hidden=true;
 const hoverLine=document.createElement('div'); hoverLine.className='synced-hover-line'; layer.appendChild(hoverLine);
 const boxes=specs.map(spec=>{{const box=document.createElement('div');box.className='synced-tooltip';layer.appendChild(box);return box;}});
 telemetryPlot.appendChild(layer);
 const format=(value,spec)=>spec.binary?(value>=50?'触发':'未触发'):`${{Number(value).toFixed(spec.digits)}} ${{spec.unit}}`;
 const setMapPosition=pointNumber=>{{
  if(mapPlot._cornerMarkerFrame)cancelAnimationFrame(mapPlot._cornerMarkerFrame);
  mapPlot._cornerMarkerFrame=requestAnimationFrame(()=>{{
   mapPlot._cornerMarkerFrame=null;
   const visible=Number.isInteger(pointNumber);
   Plotly.restyle(mapPlot,{{x:[[visible?data.rx[pointNumber]:null],[visible?data.ux[pointNumber]:null]],y:[[visible?data.ry[pointNumber]:null],[visible?data.uy[pointNumber]:null]]}},[2,3]);
  }});
 }};
 const show=pointNumber=>{{
  if(!Number.isInteger(pointNumber)||pointNumber<0||pointNumber>=data.distance.length)return;
  const distance=data.distance[pointNumber];
  const layout=telemetryPlot._fullLayout,size=layout._size,boxWidth=innerWidth<560?145:185;
  const xPixel=size.l+layout.xaxis.l2p(distance);
  const left=Math.max(4,Math.min(telemetryPlot.clientWidth-boxWidth-4,xPixel+boxWidth+18>telemetryPlot.clientWidth?xPixel-boxWidth-12:xPixel+12));
  hoverLine.style.left=`${{xPixel}}px`; hoverLine.style.top=`${{size.t}}px`; hoverLine.style.height=`${{size.h}}px`;
  specs.forEach((spec,index)=>{{
   const domain=layout[spec.axis].domain;
   const top=size.t+(1-domain[1])*size.h+3;
   const box=boxes[index]; box.style.left=`${{left}}px`; box.style.top=`${{top}}px`; box.style.width=`${{boxWidth}}px`;
   box.innerHTML=`<strong>${{spec.title}} · ${{Number(distance).toFixed(0)}} m</strong><span class="user-value">玩家 ${{format(data[spec.user][pointNumber],spec)}}</span><span class="ref-value">高手 ${{format(data[spec.ref][pointNumber],spec)}}</span>`;
  }});
  layer.hidden=false;
  setMapPosition(pointNumber);
 }};
 const hide=()=>{{layer.hidden=true;setMapPosition(null);}};
 const handleMapHover=event=>{{const point=event.points.find(item=>item.curveNumber<2);if(point)show(point.pointNumber);}};
 const handleTelemetryHover=event=>{{if(event.points.length)show(event.points[0].pointNumber);}};
 mapPlot.on('plotly_hover',handleMapHover); telemetryPlot.on('plotly_hover',handleTelemetryHover);
 mapPlot.on('plotly_unhover',hide); telemetryPlot.on('plotly_unhover',hide);
 mapPlot.onmouseleave=hide; telemetryPlot.onmouseleave=hide;
}}
function showTurn(n){{
 const d=turns[n-1]; document.querySelectorAll('.turn-button').forEach(b=>b.classList.toggle('active',+b.dataset.turn===n));
 const compact=innerWidth<560, mapHeight=compact?330:430, telHeight=compact?620:670;
 document.getElementById('cornerTitle').textContent=`${{d.name}} · ${{d.direction}}弯`;
 document.getElementById('cornerRange').textContent=`${{d.range[0].toFixed(0)}}–${{d.range[1].toFixed(0)}} m`;
 const mapPromise=Plotly.react('cornerMap',[mapLine(d.rx,d.ry,d.distance,'高手线路',R,'dash'),mapLine(d.ux,d.uy,d.distance,'我的线路',U),positionMarker('高手位置',R,18,'circle-open'),positionMarker('玩家位置',U,10,'circle')],{{height:mapHeight,margin:{{l:45,r:15,t:30,b:40}},paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{{family:'Segoe UI, Microsoft YaHei',color:INK}},legend:{{orientation:'h',y:1.08}},xaxis:{{title:'东向距离 (m)',gridcolor:GRID,scaleanchor:'y',scaleratio:1}},yaxis:{{title:'北向距离 (m)',gridcolor:GRID}},hovermode:'closest'}},cfg);
 const traces=[line(d.distance,d.userSpeed,'玩家 · 速度',U,'solid','linear',true),line(d.distance,d.refSpeed,'高手 · 速度',R,'dash','linear',true),line(d.distance,d.userBrake,'玩家 · 刹车',U),line(d.distance,d.refBrake,'高手 · 刹车',R,'dash'),line(d.distance,d.userThrottle,'玩家 · 油门',U),line(d.distance,d.refThrottle,'高手 · 油门',R,'dash'),line(d.distance,d.userSteer,'玩家 · 方向盘',U),line(d.distance,d.refSteer,'高手 · 方向盘',R,'dash'),line(d.distance,d.userGear,'玩家 · 档位',U,'solid','hv'),line(d.distance,d.refGear,'高手 · 档位',R,'dash','hv'),line(d.distance,d.userABS,'玩家 · ABS',U,'solid','hv'),line(d.distance,d.refABS,'高手 · ABS',R,'dash','hv')];
 traces.forEach((t,i)=>{{const axis=Math.floor(i/2)+1;t.xaxis=axis===1?'x':'x'+axis;t.yaxis=axis===1?'y':'y'+axis}});
 const axisLabels=[['速度','km/h',.92],['刹车','%',.74],['油门','%',.57],['方向盘','度',.40],['档位','',.24],['ABS','状态',.07]].map(([title,unit,y])=>({{xref:'paper',yref:'paper',x:-.025,y,showarrow:false,xanchor:'right',align:'right',text:`<b>${{title}}</b>${{unit?'<br>'+unit:''}}`,font:{{size:10,color:INK}}}}));
 const telemetryPromise=Plotly.react('cornerTelemetry',traces,{{height:telHeight,margin:{{l:compact?72:78,r:15,t:28,b:42}},annotations:axisLabels,paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{{family:'Segoe UI, Microsoft YaHei',color:INK,size:11}},legend:{{orientation:'h',y:1.05}},grid:{{rows:6,columns:1,pattern:'independent',roworder:'top to bottom'}},hovermode:'x',hoversubplots:'axis',hoverdistance:40,xaxis:{{showticklabels:false,gridcolor:GRID}},xaxis2:{{showticklabels:false,gridcolor:GRID,matches:'x'}},xaxis3:{{showticklabels:false,gridcolor:GRID,matches:'x'}},xaxis4:{{showticklabels:false,gridcolor:GRID,matches:'x'}},xaxis5:{{showticklabels:false,gridcolor:GRID,matches:'x'}},xaxis6:{{title:'距起终点距离 (m)',gridcolor:GRID,matches:'x'}},yaxis:{{domain:[.84,1]}},yaxis2:{{domain:[.67,.81],range:[0,102]}},yaxis3:{{domain:[.50,.64],range:[0,102]}},yaxis4:{{domain:[.33,.47]}},yaxis5:{{domain:[.18,.30],dtick:1}},yaxis6:{{domain:[0,.14],range:[-5,105],tickvals:[0,100],ticktext:['关','触发']}}}},cfg);
 Promise.all([mapPromise,telemetryPromise]).then(([mapPlot,telemetryPlot])=>setupCornerHover(mapPlot,telemetryPlot,d));
 Object.entries(d.metrics).forEach(([k,v])=>document.getElementById('m-'+k).textContent=v);
 document.getElementById('cornerAdvice').innerHTML=d.recommendations.map(x=>`<li>${{x}}</li>`).join('');
}}
document.querySelectorAll('.turn-button,.turn-link').forEach(b=>b.addEventListener('click',()=>{{showTurn(+b.dataset.turn);document.getElementById('detail').scrollIntoView({{behavior:'smooth'}})}}));
showTurn(1);
</script></body></html>"""
    OUTPUT_PATH.write_text(report, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"User {fmt_time(user_time)} | Reference {fmt_time(ref_time)} | Gap {user_time-ref_time:+.3f} s")
    print("Corner centers:", ",".join(f"{value:.5f}" for value in CORNER_CENTERS))
    print("Top losses:", ", ".join(f"{t['name']} {t['delta_time']:+.3f}s" for t in priority))


def select_single_path(explicit: str | None, candidates: list[Path], label: str) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{label} file not found: {path}")
        return path
    unique = sorted({path.resolve() for path in candidates if path.is_file()})
    if not unique:
        raise FileNotFoundError(f"No {label} file found; pass the corresponding command-line option")
    if len(unique) > 1:
        options = "\n  ".join(str(path) for path in unique)
        raise RuntimeError(f"Multiple {label} files found; select one explicitly:\n  {options}")
    return unique[0]


def parse_corner_centers(value: str | None) -> list[float]:
    if not value:
        return []
    values = []
    for token in value.split(","):
        token = token.strip()
        number = float(token[:-1]) / 100 if token.endswith("%") else float(token)
        if not 0 < number < 1:
            raise ValueError(f"Corner center must be within 0..1 (or a percentage): {token}")
        values.append(number)
    if len(set(values)) != len(values):
        raise ValueError("Corner centers must be unique")
    return sorted(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare an iRacing IBT lap with a Garage 61-style reference CSV and create an interactive HTML report."
    )
    parser.add_argument("--ibt", help="Path to the player's .ibt file")
    parser.add_argument("--reference", help="Path to the expert/reference .csv file")
    parser.add_argument("--output", help="Output HTML path; defaults to <workdir>/telemetry_analysis_report.html")
    parser.add_argument("--workdir", default=".", help="Directory used for automatic input discovery and default output")
    parser.add_argument("--reference-time", help="Reference lap time as seconds or M:SS.mmm when absent from the CSV filename")
    parser.add_argument("--corner-count", type=int, help="Override the IBT TrackNumTurns value")
    parser.add_argument(
        "--corner-centers",
        help="Override automatic turn detection with comma-separated LapDistPct values, e.g. 0.12,0.24 or 12%%,24%%",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global IBT_PATH, REF_PATH, OUTPUT_PATH, REFERENCE_TIME_OVERRIDE
    global CORNER_COUNT_OVERRIDE, CORNER_CENTERS
    args = build_parser().parse_args(argv)
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise NotADirectoryError(f"Work directory not found: {workdir}")
    IBT_PATH = select_single_path(args.ibt, list(workdir.glob("*.ibt")), "IBT")
    reference_candidates = list(workdir.glob("*.csv")) + list((workdir / "references").glob("*.csv"))
    REF_PATH = select_single_path(args.reference, reference_candidates, "reference CSV")
    OUTPUT_PATH = Path(args.output).expanduser().resolve() if args.output else workdir / "telemetry_analysis_report.html"
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_TIME_OVERRIDE = parse_time_value(args.reference_time) if args.reference_time else None
    CORNER_COUNT_OVERRIDE = args.corner_count
    CORNER_CENTERS = parse_corner_centers(args.corner_centers)
    if CORNER_COUNT_OVERRIDE and CORNER_CENTERS and CORNER_COUNT_OVERRIDE != len(CORNER_CENTERS):
        raise ValueError("--corner-count must match the number of --corner-centers")
    print(f"IBT: {IBT_PATH}")
    print(f"Reference: {REF_PATH}")
    make_report()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
