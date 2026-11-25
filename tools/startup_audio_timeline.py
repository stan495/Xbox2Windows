"""Align process-loopback packet statistics with input events on the QPC clock."""

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Packet:
    start: float
    end: float
    rms: float
    peak: float
    flags: int


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("time and level values must be finite")
    return number


def read_packets(path, origin, sample_rate):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = csv.DictReader(stream)
        required = {"process_s", "frames", "rms", "peak", "flags"}
        if not rows.fieldnames or not required.issubset(rows.fieldnames):
            raise ValueError("packet CSV needs process_s, frames, rms, peak and flags")
        packets = []
        for row in rows:
            try:
                flags = int(row["flags"])
                if flags < 0:
                    raise ValueError("negative packet flags")
                if flags & 4:  # WASAPI timestamp error
                    continue
                start = origin + finite(row["process_s"])
                frames = int(row["frames"])
                rms, peak = finite(row["rms"]), finite(row["peak"])
                if frames < 1 or not 0 <= rms <= peak <= 1:
                    raise ValueError("invalid frame count or normalized levels")
                packets.append(Packet(start, start + frames / sample_rate, rms, peak, flags))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path.name}:{rows.line_num}: {error}") from error
    return sorted(packets, key=lambda packet: packet.start)


def read_events(path, actions):
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                action = row["action"]
                if not isinstance(action, str):
                    raise ValueError("action must be a string")
                if action in actions:
                    yield action, finite(row["qpc"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path.name}:{number}: {error}") from error


def window(packets, start, end):
    cursor = start
    covered = energy = 0.0
    peak = None
    count = discontinuities = 0
    for packet in packets:
        if packet.start >= end:
            break
        first, last = max(cursor, packet.start), min(end, packet.end)
        if last <= first:
            continue
        duration = last - first
        covered += duration
        energy += packet.rms**2 * duration
        peak = packet.peak if peak is None else max(peak, packet.peak)
        count += 1
        discontinuities += bool(packet.flags & 1)
        cursor = last
    return {
        "start_qpc": start,
        "end_qpc": end,
        "coverage": min(1.0, covered / (end - start)),
        "packets": count,
        "discontinuities": discontinuities,
        "rms_estimate": math.sqrt(energy / covered) if covered else None,
        "packet_peak": peak,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packets", type=Path, help="CSV with seconds relative to process creation")
    parser.add_argument("inputs", type=Path, help="JSONL with QPC seconds and action names")
    parser.add_argument("--process-origin-qpc", required=True, type=finite)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--action", required=True, action="append", help="action to include, repeatable")
    parser.add_argument("--before", type=finite, default=0.44, help="seconds before the event")
    parser.add_argument("--after", type=finite, default=0.30, help="seconds after the event")
    args = parser.parse_args()
    if args.sample_rate < 1 or min(args.before, args.after) < 0 or args.before + args.after == 0:
        parser.error("sample rate and total window length must be positive, and endpoints nonnegative")
    try:
        packets = read_packets(args.packets, args.process_origin_qpc, args.sample_rate)
        events = [
            {"action": name, "qpc": at, **window(packets, at - args.before, at + args.after)}
            for name, at in read_events(args.inputs, set(args.action))
        ]
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"events": events}, indent=2))


if __name__ == "__main__":
    main()
