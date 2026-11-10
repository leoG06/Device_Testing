#!/usr/bin/env python3
"""
================================================================================
HP 4145 / 4145B Semiconductor Parameter Analyzer - Multi-SMU Sweep Test
================================================================================

Description:
    Runs one VAR1 sweep on the HP 4145B through the DOpeS-style driver (hp4145.py)
    with up to four SMUs. Each SMU is given a role on the command line:

        off            channel not used (output disconnected - don't leave a MOSFET gate here)
        com            0 V common / return (no data)
        sweep-v        THE swept SMU, forcing voltage, measures current   (exactly one
        sweep-i        THE swept SMU, forcing current, measures voltage    sweep SMU)
        v:VALUE[:COMP] constant voltage VALUE (V), current compliance COMP (A), measures current
        i:VALUE[:COMP] constant current VALUE (A), voltage compliance COMP (V), measures voltage

    Sweep values (--start/--stop/--step) are in volts for sweep-v and in amperes
    for sweep-i. --compliance is the swept SMU's limit: amperes for sweep-v, volts
    for sweep-i.

    Data output: every measured channel is printed (auto-scaled units) and saved to
    one CSV: the programmed sweep value first, then one column per measured channel in
    SMU order (e.g. V1_set_V, I1_A, I3_A), written with 6 significant figures. Status
    flags (C compliance, X oscillation, ...) are listed on screen and saved to
    <csv>_flags.csv when any point is flagged. --tlm-csv also writes the old
    two-column "Voltage,Current" file (sweep-v only) for the TLM pipeline.

Usage Examples:
    # 1. Two-terminal I-V, same as test_voltage_sweep.py defaults (SMU1 sweeps, SMU2 common):
    python parameter_analyzer_sweep.py --start -1 --stop 1 --step 0.05 --compliance 10e-3

    # 2. MOSFET output curve: drain SMU1 swept 0-3 V, gate SMU3 at 1.5 V, source SMU2 common:
    python parameter_analyzer_sweep.py --smu1 sweep-v --smu2 com --smu3 v:1.5:1e-6 --start 0 --stop 3 --step 0.05 --compliance 20e-3

    # 3. MOSFET transfer curve: gate SMU3 swept, drain SMU1 at 0.1 V:
    python parameter_analyzer_sweep.py --smu1 v:0.1:20e-3 --smu2 com --smu3 sweep-v --start -1 --stop 3 --step 0.05 --compliance 1e-6

    # 4. BJT output curve: collector SMU1 swept, base SMU3 forced at 10 uA, emitter SMU2 common:
    python parameter_analyzer_sweep.py --smu1 sweep-v --smu2 com --smu3 i:10e-6:2 --start 0 --stop 5 --step 0.1 --compliance 50e-3

    # 5. Diode forward voltage vs current (current sweep, log 10 points/decade):
    python parameter_analyzer_sweep.py --smu1 sweep-i --smu2 com --start 1e-9 --stop 1e-2 --sweep-type log --compliance 2

    # 6. Offline demo (built-in simulator: every active SMU tied to one node through 1 kOhm):
    python parameter_analyzer_sweep.py --smu1 sweep-v --smu2 com --smu3 v:0.5 --mock
================================================================================
"""

import sys
import os
import argparse
import datetime
import re
import time
import numpy as np

# ------------------------------------------------------------------------------
# Path setup
# ------------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")
CSV_DIR = os.path.join(PROJECT_ROOT, "CSV")
for _p in (PROJECT_ROOT, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SMUS = [1, 2, 3, 4]

# Default compliance when a constant source is given without one
DEFAULT_COMP = {"voltage": 1e-3, "current": 2.0}   # 1 mA for V sources, 2 V for I sources

STATUS_MEANING = {"N": "normal", "L": "interval too short", "V": "A/D converter saturation",
                  "X": "oscillation", "C": "this channel reached compliance",
                  "T": "another channel reached compliance"}


# ==============================================================================
# Command line
# ==============================================================================
def parse_arguments():
    parser = argparse.ArgumentParser(
        description="HP 4145 / 4145B multi-SMU sweep (up to 4 SMUs, voltage or current forcing)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="SMU roles: off | com | sweep-v | sweep-i | v:VALUE[:COMP] | i:VALUE[:COMP]\n"
               "  v:1.5:1e-3  -> constant 1.5 V, 1 mA compliance (measures current)\n"
               "  i:10e-6:2   -> constant 10 uA, 2 V compliance  (measures voltage)\n"
               "Default compliance if omitted: 1 mA for v:, 2 V for i:.",
    )

    parser.add_argument("--address", type=str, default="GPIB0::16::INSTR",
                        help="VISA resource string (default: %(default)s)")

    # SMU roles
    roles = parser.add_argument_group("SMU roles")
    roles.add_argument("--smu1", type=str, default="sweep-v", help="role of SMU1 (default: %(default)s)")
    roles.add_argument("--smu2", type=str, default="com", help="role of SMU2 (default: %(default)s)")
    roles.add_argument("--smu3", type=str, default="off", help="role of SMU3 (default: %(default)s)")
    roles.add_argument("--smu4", type=str, default="off", help="role of SMU4 (default: %(default)s)")

    # Sweep
    sweep = parser.add_argument_group("sweep (applies to the sweep-v / sweep-i SMU)")
    sweep.add_argument("--start", type=float, default=None,
                       help="start value: V for sweep-v, A for sweep-i (sweep-v default: -2)")
    sweep.add_argument("--stop", type=float, default=None,
                       help="stop value: V for sweep-v, A for sweep-i (sweep-v default: 2)")
    sweep.add_argument("--step", type=float, default=None,
                       help="step (linear only): V or A (sweep-v default: 0.1)")
    sweep.add_argument("--sweep-type", type=str, default="linear", choices=["linear", "log"],
                       help="linear, or log = 10 points/decade (default: %(default)s)")
    sweep.add_argument("--compliance", type=float, default=None,
                       help="compliance of the swept SMU: A for sweep-v (default 1e-3), "
                            "V for sweep-i (default 2)")

    # Timing
    timing = parser.add_argument_group("timing")
    timing.add_argument("--integration", type=str, default="M", choices=["S", "M", "L"],
                        help="S = 3.6 ms, M = 1 power-line cycle, L = 16 cycles (default: %(default)s)")
    timing.add_argument("--delay", type=float, default=0.01,
                        help="delay per step before measuring, 0-6.5 s (default: %(default)s)")
    timing.add_argument("--hold", type=float, default=0.0,
                        help="hold time before the sweep starts, 0-650 s (default: %(default)s)")

    # Output
    out = parser.add_argument_group("output")
    out.add_argument(
        "--save-csv",
        nargs="?",
        const="CSV/hardware_sweep.csv",
        default="",
        metavar="PATH",
        help="only save CSV when passed (optional filename inside CSV/, default: CSV/hardware_sweep.csv)",
    )
    out.add_argument("--check-visa", action="store_true",
                     help="run the VISA diagnostic scan (test_visa_connection.py) first")
    out.add_argument("--mock", action="store_true",
                     help="offline simulator instead of the instrument")

    return parser.parse_args()


def parse_role(text, smu):
    """Turn an SMU role string into a dict(kind, mode, value, comp)."""
    t = text.strip().lower()
    if t == "off":
        return dict(kind="off")
    if t == "com":
        return dict(kind="com")
    if t in ("sweep-v", "sweep-i"):
        return dict(kind="sweep", mode="voltage" if t.endswith("v") else "current")
    m = re.fullmatch(r"([vi]):([^:]+)(?::([^:]+))?", t)
    if not m:
        raise ValueError(f"--smu{smu} '{text}': use off, com, sweep-v, sweep-i, v:VALUE[:COMP] or i:VALUE[:COMP]")
    mode = "voltage" if m.group(1) == "v" else "current"
    try:
        value = float(m.group(2))
        comp = float(m.group(3)) if m.group(3) else DEFAULT_COMP[mode]
    except ValueError:
        raise ValueError(f"--smu{smu} '{text}': value and compliance must be numbers")
    return dict(kind="const", mode=mode, value=value, comp=comp, comp_default=m.group(3) is None)


# ==============================================================================
# Limits from the manual (Table 1-1): SMU output/compliance pairs
# ==============================================================================
def max_current_at(v):
    """Largest current an SMU can deliver at |v| volts (20 V: 100 mA, 40 V: 50 mA, 100 V: 20 mA)."""
    v = abs(v)
    if v <= 20:
        return 0.1
    if v <= 40:
        return 0.05
    if v <= 100:
        return 0.02
    return 0.0


def check_source(label, mode, values, comp):
    """Raise ValueError if a source setting is outside what an SMU can do."""
    vmax = max(abs(x) for x in values)
    if mode == "voltage":
        if vmax > 100:
            raise ValueError(f"{label}: {vmax} V exceeds the SMU maximum of 100 V")
        if not (0 < comp <= 0.1):
            raise ValueError(f"{label}: current compliance must be > 0 and <= 0.1 A")
        if comp > max_current_at(vmax):
            raise ValueError(f"{label}: {comp} A compliance not allowed at {vmax} V "
                             f"(max {max_current_at(vmax)} A; 100 mA <= 20 V, 50 mA <= 40 V, 20 mA <= 100 V)")
    else:
        if vmax > 0.1:
            raise ValueError(f"{label}: {vmax} A exceeds the SMU maximum of 100 mA")
        if not (0 < comp <= 100):
            raise ValueError(f"{label}: voltage compliance must be > 0 and <= 100 V")
        if vmax > max_current_at(comp):
            raise ValueError(f"{label}: {vmax} A not allowed with a {comp} V compliance "
                             f"(max {max_current_at(comp)} A)")


def build_setup(args):
    """Validate the roles and turn them into the dictionaries hp4145.initialize() expects."""
    roles = {n: parse_role(getattr(args, f"smu{n}"), n) for n in SMUS}
    sweepers = [n for n in SMUS if roles[n]["kind"] == "sweep"]
    if len(sweepers) != 1:
        raise ValueError(f"exactly one SMU must be sweep-v or sweep-i (found {len(sweepers)})")
    master = sweepers[0]
    mmode = roles[master]["mode"]

    # sweep values: defaults only for voltage sweeps (a current default could be dangerous)
    if mmode == "voltage":
        start = -2.0 if args.start is None else args.start
        stop = 2.0 if args.stop is None else args.stop
        step = 0.1 if args.step is None else args.step
        comp = 1e-3 if args.compliance is None else args.compliance
    else:
        if args.start is None or args.stop is None or (args.sweep_type == "linear" and args.step is None):
            raise ValueError("sweep-i needs --start and --stop (and --step for linear) in amperes")
        start, stop = args.start, args.stop
        step = args.step if args.step is not None else 0.0
        comp = 2.0 if args.compliance is None else args.compliance

    if args.sweep_type == "linear":
        if step == 0:
            raise ValueError("--step must not be 0")
        n_points = int(round(abs(stop - start) / abs(step))) + 1
        if abs((stop - start) / step - round((stop - start) / step)) > 1e-6:
            raise ValueError("(stop - start) must be a whole number of steps")
    else:
        if start == 0 or stop == 0 or np.sign(start) != np.sign(stop):
            raise ValueError("log sweep needs non-zero --start and --stop of the same sign")
        n_points = int(np.floor(round(10 * abs(np.log10(stop / start)), 6))) + 1
    if n_points > 1024:
        raise ValueError(f"{n_points} points: the 4145B allows at most 1024 VAR1 steps")

    check_source(f"SMU{master} (sweep)", mmode, [start, stop], comp)
    for n in SMUS:
        r = roles[n]
        if r["kind"] == "const":
            check_source(f"SMU{n}", r["mode"], [r["value"]], r["comp"])

    smu_type = {f"SMU{n}": "common" for n in SMUS}
    smu_used = {f"SMU{n}": "off" for n in SMUS}
    smu_bias = {f"SMU{n}": 0.0 for n in SMUS}
    smu_comp = {f"SMU{n}": 1e-3 for n in SMUS}
    for n in SMUS:
        r, key = roles[n], f"SMU{n}"
        if r["kind"] == "off":
            continue
        smu_used[key] = "on"
        if r["kind"] == "com":
            smu_type[key] = "common"
        elif r["kind"] == "sweep":
            smu_type[key], smu_comp[key] = mmode, comp
        else:
            smu_type[key], smu_bias[key], smu_comp[key] = r["mode"], r["value"], r["comp"]

    return dict(roles=roles, master=master, mode=mmode, start=start, stop=stop, step=step,
                comp=comp, n_points=n_points, smu_type=smu_type, smu_used=smu_used,
                smu_bias=smu_bias, smu_comp=smu_comp)


# ==============================================================================
# Formatting helpers
# ==============================================================================
def unit_of(name):
    return "V" if name.upper().startswith("V") else "A"


def si_scale(values, unit):
    """Factor and label so the largest |value| reads between 1 and 1000."""
    m = np.nanmax(np.abs(values)) if np.size(values) else 0.0
    for factor, prefix in [(1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")]:
        if m >= factor:
            return factor, prefix + unit
    return 1e-15, "f" + unit


def describe_role(r, n, setup):
    if r["kind"] == "off":
        return "off (not used)"
    if r["kind"] == "com":
        return "COMMON (0 V return, no data)"
    if r["kind"] == "sweep":
        u = "V" if r["mode"] == "voltage" else "A"
        cu = "A" if r["mode"] == "voltage" else "V"
        rng = (f"{setup['start']:g} -> {setup['stop']:g} {u}, step {setup['step']:g} {u}"
               if setup["n_points"] and setup["step"] else f"{setup['start']:g} -> {setup['stop']:g} {u}, log")
        meas = "current" if r["mode"] == "voltage" else "voltage"
        return f"SWEEP {r['mode']} ({rng}), compliance {setup['comp']:g} {cu}, measures {meas}"
    u = "V" if r["mode"] == "voltage" else "A"
    cu = "A" if r["mode"] == "voltage" else "V"
    meas = "current" if r["mode"] == "voltage" else "voltage"
    note = " (default)" if r.get("comp_default") else ""
    return f"constant {r['value']:g} {u}, compliance {r['comp']:g} {cu}{note}, measures {meas}"


# ==============================================================================
# Main
# ==============================================================================
def main():
    args = parse_arguments()
    try:
        setup = build_setup(args)
    except ValueError as e:
        sys.exit(f"[SETUP ERROR] {e}")

    roles, master = setup["roles"], setup["master"]

    # ---------------------------------------------------------------- banner
    print("=" * 80)
    print(" HP 4145 / 4145B SEMICONDUCTOR PARAMETER ANALYZER - MULTI-SMU SWEEP")
    print("=" * 80)
    print(f" • Target VISA Address : {args.address}{'  [MOCK]' if args.mock else ''}")
    for n in SMUS:
        print(f" • SMU{n}                : {describe_role(roles[n], n, setup)}")
    print(f" • Sweep               : {args.sweep_type}, {setup['n_points']} points")
    print(f" • Integration / delay : {args.integration} / {args.delay:g} s, hold {args.hold:g} s")
    print(f" • Output CSV          : {args.save_csv or 'None (display only)'}")
    if setup["n_points"] > 512:
        print("   ! more than 512 points: above the single-sweep spec (Table 1-1); the 4145B may refuse it")
    vmax_all = [abs(setup["start"]), abs(setup["stop"])] if setup["mode"] == "voltage" else []
    vmax_all += [abs(r["value"]) for r in roles.values() if r["kind"] == "const" and r["mode"] == "voltage"]
    vmax_all += [r["comp"] for r in roles.values() if r["kind"] == "const" and r["mode"] == "current"]
    if setup["mode"] == "current":
        vmax_all.append(setup["comp"])
    if vmax_all and max(vmax_all) > 42:
        print("   ! voltages above 42 V possible: the fixture lid must be closed (or the shorting")
        print("     connector fitted) or the 4145B will not run the measurement")
    print("-" * 80)

    if args.check_visa and not args.mock:
        from tests.test_visa_connection import check_visa_connection
        check_visa_connection(target_address=args.address)
        print("-" * 80)

    # ---------------------------------------------------------------- connect
    from tests.hp4145 import hp4145
    if args.mock:
        analyzer = hp4145.__new__(hp4145)          # skip the VISA connection
        analyzer.pyvisa_resource = MockResource()
    else:
        print(f"Connecting to HP 4145 at '{args.address}' ...")
        try:
            analyzer = hp4145(args.address, timeout=15000)
        except Exception as e:
            print(f"\n[CONNECTION ERROR] Could not connect to '{args.address}': {e}")
            print("  - Run 'python test_visa_connection.py' to scan GPIB addresses.")
            print("  - Run with --mock to test the script without the instrument.")
            sys.exit(1)

    try:
        # ------------------------------------------------------------ configure
        print("Sending configuration to the 4145B ...")
        analyzer.initialize(
            smu_type=setup["smu_type"], smu_used=setup["smu_used"], smu_master=master,
            smu_bias=setup["smu_bias"], smu_compliance=setup["smu_comp"],
            sweep_param={"start": setup["start"], "stop": setup["stop"], "step": setup["step"]},
            sweep_type=args.sweep_type, integration_mode=args.integration,
            delay_time=args.delay, hold_time=args.hold,
        )

        # ------------------------------------------------------------ measure
        print("Running the sweep ...")
        t0 = time.time()
        data, header = analyzer.launch_measurements()
        print(f"Acquisition completed in {time.time() - t0:.2f} s.")
        status = analyzer.last_status
    finally:
        try:
            analyzer.close_connection()
            print("VISA connection closed.")
        except Exception:
            pass

    # ---------------------------------------------------------------- data output
    names = [h.strip() for h in header.split(",")]          # e.g. ["V1", "I1", "I3"]
    sweep_name, meas_names = names[0], names[1:]
    sweep_unit = unit_of(sweep_name)

    # console table, each column in its own SI unit
    cols = [(f"{sweep_name} set", data[:, 0], sweep_unit)]
    cols += [(nm, data[:, k + 1], unit_of(nm)) for k, nm in enumerate(meas_names)]
    scaled = []
    for title, vals, unit in cols:
        f, lab = si_scale(vals, unit)
        scaled.append((f"{title} ({lab})", vals / f))
    width = 16
    line = f"{'Point':>6} | " + " | ".join(f"{t:>{width}}" for t, _ in scaled) + " | flags"
    print("\n" + "=" * len(line))
    print(" MEASUREMENT RESULTS")
    print("=" * len(line))
    print(line)
    print("-" * len(line))
    for p in range(data.shape[0]):
        flags = " ".join(f"{nm}:{status[p, k]}" for k, nm in enumerate(meas_names) if status[p, k] != "N")
        print(f"{p + 1:6d} | " + " | ".join(f"{v[p]:>{width}.5g}" for _, v in scaled) + f" | {flags}")
    print("-" * len(line))

    # per-channel summary
    print("\nChannel summary:")
    for k, nm in enumerate(meas_names):
        vals, st = data[:, k + 1], status[:, k]
        f, lab = si_scale(vals, unit_of(nm))
        n_bad = int((st != "N").sum())
        letters = ", ".join(f"{s} = {STATUS_MEANING.get(s, '?')}" for s in sorted(set(st[st != "N"])))
        print(f"  {nm:<4} min {vals.min() / f:+.5g} {lab}, max {vals.max() / f:+.5g} {lab}"
              + (f", {n_bad} flagged ({letters})" if n_bad else ", all points normal"))

    # differential resistance for a plain 2-terminal voltage sweep
    active = [n for n in SMUS if roles[n]["kind"] != "off"]
    master_meas = ("I%d" if setup["mode"] == "voltage" else "V%d") % master
    if len(active) == 2 and master_meas in meas_names:
        k = meas_names.index(master_meas)
        ok = status[:, k] == "N"
        if ok.sum() >= 2:
            x, y = data[ok, 0], data[ok, k + 1]
            if setup["mode"] == "voltage":
                slope = np.polyfit(x, y, 1)[0]
                r = 1 / slope if slope else np.inf
            else:
                r = np.polyfit(x, y, 1)[0]
            print(f"  2-terminal differential resistance (unflagged points): {r:.6g} Ω")

    # main CSV: sweep value + every measured channel, 6 significant figures
    if args.save_csv:
        raw_path = args.save_csv.strip()
        if os.path.isabs(raw_path):
            path = os.path.abspath(raw_path)
        else:
            parts = os.path.normpath(raw_path).split(os.sep)
            while parts and parts[0] in (".", "CSV", "tests"):
                parts = parts[1:]
            rel = os.path.join(*parts) if parts else "hardware_sweep.csv"
            path = os.path.abspath(os.path.join(CSV_DIR, rel))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cols_hdr = [f"{sweep_name}_set_{sweep_unit}"] + [f"{nm}_{unit_of(nm)}" for nm in meas_names]
        with open(path, "w") as f:
            f.write(",".join(cols_hdr) + "\n")
            for row in data:
                f.write(",".join(f"{x:.6E}" for x in row) + "\n")
        print(f"\nData CSV saved to: {path}")

        # setup notes next to it (roles, sweep, timing, date)
        meta = os.path.splitext(path)[0] + "_setup.txt"
        with open(meta, "w", encoding="utf-8") as f:
            f.write(f"Measured {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            for n in SMUS:
                f.write(f"SMU{n}: {describe_role(roles[n], n, setup)}\n")
            f.write(f"sweep type: {args.sweep_type}, points: {setup['n_points']}\n")
            f.write(f"integration: {args.integration}, delay: {args.delay} s, hold: {args.hold} s\n")
            f.write("columns: " + ", ".join(cols_hdr) + "\n")
            f.write("sign: positive current flows out of an SMU into the device\n")
        print(f"Setup notes saved to: {meta}")

        if (status != "N").any():
            flag_path = os.path.splitext(path)[0] + "_flags.csv"
            with open(flag_path, "w") as f:
                f.write("point,sweep_value,channel,flag,meaning\n")
                for p in range(data.shape[0]):
                    for k, nm in enumerate(meas_names):
                        if status[p, k] != "N":
                            f.write(f"{p + 1},{data[p, 0]:.6E},{nm},{status[p, k]},"
                                    f"{STATUS_MEANING.get(status[p, k], '?')}\n")
            print(f"Flagged points saved to: {flag_path}")


# ==============================================================================
# Offline simulator for --mock
# ==============================================================================
class MockResource:
    """Fake 4145B: every active SMU is tied to one internal node through 1 kOhm.
    Understands the System-Mode codes hp4145.py sends (CH, VR/IR, VC/IC, LI, ME1, DO)."""

    R = 1e3

    def __init__(self):
        self.mode = {}        # smu -> 1 V, 2 I, 3 COM
        self.const = {}       # smu -> (value, comp)
        self.master = None
        self.sweep = None     # (values, comp)
        self.page = None
        self.ready = False
        self.result = {}
        self.timeout = 0

    def write(self, cmd):
        c = cmd.strip()
        if c in ("DE", "SS", "SM", "MD"):
            self.page = c
        m = re.fullmatch(r"CH(\d)(?:, '\w+', '\w+', (\d), (\d))?", c)
        if m:
            n = int(m.group(1))
            if m.group(2) is None:
                self.mode.pop(n, None)
            else:
                self.mode[n] = int(m.group(2))
                if m.group(3) == "1":
                    self.master = n
        m = re.fullmatch(r"([VI])R(\d), ([^,]+), ([^,]+), ([^,]+)(?:, ([^,]+))?", c)
        if m and self.page == "SS":
            kind, sm = m.group(1), int(m.group(2))
            a, b = float(m.group(3)), float(m.group(4))
            if sm == 1:
                st, comp = float(m.group(5)), float(m.group(6))
                vals = a + st * np.arange(int(round((b - a) / st)) + 1)
            else:
                comp = float(m.group(5))
                n = int(np.floor(round(10 * abs(np.log10(b / a)), 6))) + 1
                vals = a * 10 ** (np.sign(np.log10(b / a)) * np.arange(n) / 10)
            self.sweep = (vals, comp)
        m = re.fullmatch(r"[VI]C(\d), ([^,]+), ([^,]+)", c)
        if m:
            self.const[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        if c == "ME1":
            self._solve()
            self.ready = True

    def _solve(self):
        vals, comp_m = self.sweep
        res = {}
        for x in vals:
            v_src, i_src = {}, {}
            for n, md in self.mode.items():
                val = x if n == self.master else (0.0 if md == 3 else self.const.get(n, (0.0, 0))[0])
                (v_src if md in (1, 3) else i_src)[n] = val
            g = len(v_src) / self.R
            node = (sum(v / self.R for v in v_src.values()) + sum(i_src.values())) / g if g else 0.0
            for n, md in self.mode.items():
                comp = comp_m if n == self.master else self.const.get(n, (0, 1e9))[1]
                if md == 1:
                    i = (v_src[n] - node) / self.R
                    flag = "C" if abs(i) >= comp else "N"
                    res.setdefault(f"I{n}", []).append((flag, float(np.clip(i, -comp, comp))))
                elif md == 2:
                    v = node + i_src[n] * self.R
                    flag = "C" if abs(v) >= comp else "N"
                    res.setdefault(f"V{n}", []).append((flag, float(np.clip(v, -comp, comp))))
        self.result = res

    def read_stb(self):
        r, self.ready = self.ready, False
        return 1 if r else 0

    def query(self, cmd):
        name = re.search(r"'(\w+)'", cmd).group(1)
        return ",".join(f"{f}{v:+.4E}" for f, v in self.result[name]) + "\r\n"

    def close(self):
        pass


if __name__ == "__main__":
    main()
