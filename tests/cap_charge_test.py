#!/usr/bin/env python3
"""
HP 4145B - capacitance from constant-current charging (time-domain mode)
========================================================================

The 4145B cannot measure capacitance directly (it is a DC instrument). This script uses
its TIME DOMAIN mode (manual para. 3-38/3-39, Fig. 3-35) instead:

    SMU1 forces a small constant current I into the device (current mode, CONST),
    SMU2 is common (0 V), and SMU1's own voltage is read every INTERVAL seconds.
    For a capacitor  I = C * dV/dt   ->   C = I / (dV/dt)

The time delay between readings is the instrument's INTERVAL (10 ms - 10 s, 10 ms
resolution), not a Python sleep, so the timing is accurate.

Sequence (default, --bipolar):
    1. discharge   : SMU1 at 0 V for a moment (clears any stored charge)
    2. charge  +I  : V rises until it reaches the voltage compliance (--vmax)
    3. discharge
    4. charge  -I  : V falls toward -vmax
    5. discharge
The +I and -I slopes together cancel a constant leakage current:
    C      = 2 I / (slope_up - slope_down)
    I_leak = -C (slope_up + slope_down) / 2
(valid when C and leakage are about the same at +V and -V; for MOS capacitors or
junctions they are not - use the single-polarity C(V) curves instead).

Limits (manual Table 1-1 / 1-2):
    - SMU load capacitance spec: <= 1000 pF. Larger capacitors usually charge fine in
      current mode, but specs are not guaranteed - watch for status flag X (oscillation).
    - Smallest useful C is set by leakage: SMU voltmeter offset current ~6 pA, fixture and
      device leakage. Keep I >= ~50x the leakage. Below ~10-20 pF this gets hard.
    - Fixture/probe/cable stray capacitance adds to the result: run once with the probes
      lifted (same settings) and subtract that value.

Usage:
    python cap_charge_test.py --address "GPIB0::16::INSTR" --current 100e-12 --vmax 1
    python cap_charge_test.py --mock          # offline demo: 220 pF with 50 GOhm leakage
"""

import argparse
import os
import re
import sys
import time
import numpy as np

STATUS_MEANING = {"N": "normal", "L": "interval too short", "V": "A/D saturation",
                  "X": "oscillation", "C": "compliance reached", "T": "other channel in compliance"}


def parse_arguments():
    p = argparse.ArgumentParser(description="HP 4145B constant-current capacitance test",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--address", default="GPIB0::16::INSTR")
    p.add_argument("--force-smu", type=int, default=1, choices=[1, 2, 3, 4], help="SMU that forces current")
    p.add_argument("--common-smu", type=int, default=2, choices=[1, 2, 3, 4], help="SMU used as 0 V common")
    p.add_argument("--current", type=float, default=100e-12, help="charging current (A), magnitude")
    p.add_argument("--vmax", type=float, default=1.0,
                   help="voltage compliance = highest voltage the device will see (V)")
    p.add_argument("--interval", type=float, default=0.05, help="time between readings (s), 0.01-10")
    p.add_argument("--points", type=int, default=200, help="readings per charge run, 1-1024")
    p.add_argument("--wait", type=float, default=0.0, help="WAIT TIME before the first reading (s)")
    p.add_argument("--discharge", type=float, default=1.0, help="discharge time at 0 V before each run (s)")
    p.add_argument("--unipolar", action="store_true", help="only charge with +I (no leakage cancellation)")
    p.add_argument("--save-csv", default="CAP/cap_charge.csv", help="raw data file ('' to skip)")
    p.add_argument("--mock", action="store_true", help="offline demo with a simulated capacitor")
    return p.parse_args()


# ----------------------------------------------------------------------------- instrument
class HP4145TimeDomain:
    """Minimal System-Mode driver for time-domain runs (HP-IB codes from manual Fig. 3-52)."""

    def __init__(self, resource):
        self.res = resource

    def w(self, cmd):
        self.res.write(cmd)

    def _setup_channels(self, force, common, mode):
        # mode: 1 = V, 2 = I ; source function 3 = CONST (no VAR1 -> time domain)
        self.w("BC")
        self.w("DR1")
        self.w("DE")
        for n in range(1, 5):
            if n == force:
                self.w("CH%d, 'V%d', 'I%d', %d, 3" % (n, n, n, mode))
            elif n == common:
                self.w("CH%d, 'V%d', 'I%d', 3, 3" % (n, n, n))
            else:
                self.w("CH%d" % n)
        self.w("VS1; VS2; VM1; VM2")

    def _time_domain(self, wait, interval, points, monitor):
        self.w("SM")
        self.w("WT %.2f" % wait)
        self.w("IN %.2f" % interval)
        self.w("NR %d" % points)
        self.w("DM2")
        self.w("LI '%s'" % monitor)
        self.w("BC")
        self.res.read_stb()
        self.w("MD")
        self.w("ME1")
        self._wait_ready(30 + wait + 3 * points * interval)
        raw = self.res.query("DO '%s'" % monitor).strip()
        fields = [f.strip() for f in raw.split(",") if f.strip()]
        status = np.array([f[0] for f in fields])
        values = np.array([float(f[1:]) for f in fields])
        t = wait + interval * np.arange(len(values))
        return t, values, status

    def _wait_ready(self, limit):
        t0 = time.time()
        while True:
            stb = self.res.read_stb()
            if stb & 0x80:
                raise RuntimeError("4145B emergency (status byte %d): lid open / SMU shut down?" % stb)
            if stb & 0x08:
                raise RuntimeError("4145B rejected the setup (Illegal Program) - see the CRT message")
            if stb & 0x01:
                return
            if time.time() - t0 > limit:
                raise TimeoutError("no Data Ready after %.0f s" % limit)
            time.sleep(0.05)

    def discharge(self, force, common, seconds, i_comp=1e-3):
        """Hold the force SMU at 0 V (voltage mode) and return the current it measured."""
        self._setup_channels(force, common, mode=1)
        self.w("SS")
        self.w("VC%d, 0, %.6E" % (force, i_comp))
        n = max(2, int(round(seconds / 0.1)))
        return self._time_domain(0.0, 0.1, n, "I%d" % force)

    def charge(self, force, common, current, vmax, wait, interval, points):
        """Force a constant current and read the voltage of the force SMU vs time."""
        self._setup_channels(force, common, mode=2)
        self.w("SS")
        self.w("IC%d, %.6E, %.6E" % (force, current, vmax))
        return self._time_domain(wait, interval, points, "V%d" % force)


# ----------------------------------------------------------------------------- analysis
def fit_slope(t, v, status, vmax):
    """Linear fit of V(t) over the unclamped part of the run (status N, |V| < 95 % of vmax)."""
    ok = (status == "N") & (np.abs(v) < 0.95 * vmax)
    # skip the first reading: it contains the current-source turn-on step
    ok[:1] = False
    if ok.sum() < 3:
        return np.nan, ok
    slope = np.polyfit(t[ok], v[ok], 1)[0]
    return slope, ok


def rate_vs_voltage(t, v, ok, window=7):
    """Local dV/dt (sliding linear fit) at each usable reading -> (V, dV/dt)."""
    idx = np.where(ok)[0]
    h = window // 2
    vv, ss = [], []
    for k in range(h, len(idx) - h):
        sel = idx[k - h:k + h + 1]
        ss.append(np.polyfit(t[sel], v[sel], 1)[0])
        vv.append(v[idx[k]])
    return np.array(vv), np.array(ss)


def fit_rc(t, v, ok, window=7):
    """Fit dV/dt = a + b*V.  For C with parallel leakage R and offset current I0:
         a = (I - I0)/C   (rate at 0 V)      b = -1/(R C)
       Returns a, b, and the relative scatter of the fit (large -> non-linear device)."""
    vv, ss = rate_vs_voltage(t, v, ok, window)
    if len(vv) < 4:
        return np.nan, np.nan, np.nan
    b, a = np.polyfit(vv, ss, 1)
    scatter = np.std(ss - (a + b * vv)) / max(abs(a), 1e-30)
    return a, b, scatter


def local_cv(t, v, ok, current, window=7):
    """Quasi-static C(V): C = I / (dV/dt) from a sliding linear fit."""
    idx = np.where(ok)[0]
    out_v, out_c = [], []
    h = window // 2
    for k in range(h, len(idx) - h):
        sel = idx[k - h:k + h + 1]
        s = np.polyfit(t[sel], v[sel], 1)[0]
        if s != 0:
            out_v.append(v[idx[k]])
            out_c.append(current / s)
    return np.array(out_v), np.array(out_c)


def fmt_f(c):
    if not np.isfinite(c):
        return "n/a"
    for factor, prefix in [(1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")]:
        if abs(c) >= factor:
            return f"{c / factor:.4g} {prefix}F"
    return f"{c:.3g} F"


# ----------------------------------------------------------------------------- main
def main(a=None):
    """Run the test. `a` can be any argparse.Namespace with the same fields as
    parse_arguments() (address, force_smu, common_smu, current, vmax, interval, points,
    wait, discharge, unipolar, save_csv, mock) - e.g. from test_voltage_sweep.py --mode cap."""
    if a is None:
        a = parse_arguments()
    if not (0.01 <= a.interval <= 10):
        sys.exit("--interval must be 0.01 - 10 s (manual Fig. 3-35)")
    if not (1 <= a.points <= 1024):
        sys.exit("--points must be 1 - 1024")
    if a.force_smu == a.common_smu:
        sys.exit("--force-smu and --common-smu must differ")

    window = a.wait + a.interval * (a.points - 1)
    c_full = a.current * window / a.vmax
    print("=" * 78)
    print(" HP 4145B constant-current capacitance test (time-domain mode)")
    print("=" * 78)
    print(f" Force SMU{a.force_smu} (current), common SMU{a.common_smu}")
    print(f" I = ±{a.current:.3e} A, voltage limit ±{a.vmax:g} V, "
          f"{a.points} readings every {a.interval:g} s ({window:.2f} s window)")
    print(f" A capacitor of {fmt_f(c_full)} reaches {a.vmax:g} V exactly at the end of the window;")
    print(f" smaller ones reach it sooner (need >= ~10 unclamped readings: C >= {fmt_f(c_full * 10 / a.points)}).")
    print("-" * 78)

    if a.mock:
        res = MockResource(c=220e-12, r_leak=50e9, stray=3e-12)
        print("[MOCK] simulated 220 pF, 50 GΩ leakage, 3 pF stray\n")
    else:
        import pyvisa
        res = pyvisa.ResourceManager().open_resource(a.address)
        res.timeout = 20000
    inst = HP4145TimeDomain(res)

    runs = []
    signs = [+1] if a.unipolar else [+1, -1]
    try:
        for sgn in signs:
            _, i_dis, _ = inst.discharge(a.force_smu, a.common_smu, a.discharge)
            print(f"Discharged (current at 0 V at end: {i_dis[-1]:+.2e} A)")
            t, v, st = inst.charge(a.force_smu, a.common_smu, sgn * a.current, a.vmax,
                                   a.wait, a.interval, a.points)
            slope, ok = fit_slope(t, v, st, a.vmax)
            ra, rb, scat = fit_rc(t, v, ok)
            bad = sorted(set(st[(st != "N") & (st != "C")]))
            print(f"Charge {'+' if sgn > 0 else '-'}I: {ok.sum()} usable readings, "
                  f"dV/dt at 0 V = {ra:+.4e} V/s -> C = {fmt_f(sgn * a.current / ra) if ra else 'n/a'}"
                  + (f"   flags: {', '.join(f'{b} ({STATUS_MEANING.get(b, b)})' for b in bad)}" if bad else ""))
            runs.append(dict(sign=sgn, t=t, v=v, st=st, slope=slope, ok=ok, a=ra, b=rb, scatter=scat))
        inst.discharge(a.force_smu, a.common_smu, a.discharge)  # leave the device discharged
    finally:
        try:
            res.close()
        except Exception:
            pass

    # ------------------------------------------------------------------- result
    print("\n" + "=" * 30 + " RESULT " + "=" * 40)
    for r in runs:
        r["clamped"] = bool((r["st"] == "C").any() or (np.abs(r["v"]) >= 0.95 * a.vmax).any())
    few = [r for r in runs if r["ok"].sum() < 10]
    if few:
        if any(r["clamped"] for r in few):
            print(" ! Reached the voltage limit after only a few readings: C is small for these settings")
            print("   -> lower --current and/or shorten --interval (aim for 20+ readings before the limit).")
        else:
            print(" ! Too few usable readings for a reliable fit (check status flags above).")
    good = [r for r in runs if np.isfinite(r["a"])]
    if not a.unipolar and len(good) == 2:
        a_up, a_dn = runs[0]["a"], runs[1]["a"]
        c = 2 * a.current / (a_up - a_dn)           # cancels a constant offset current
        i0 = -c * (a_up + a_dn) / 2
        print(f" C                      : {fmt_f(c)}   (from dV/dt at 0 V, ±I averaged)")
        print(f" Offset/leak current    : {i0:+.2e} A at 0 V")
        c_up, c_dn = a.current / a_up, -a.current / a_dn
        if abs(c_up - c_dn) / abs(c) > 0.2:
            print(f" ! +I and -I disagree by >20 % ({fmt_f(c_up)} vs {fmt_f(c_dn)}): C depends on")
            print("   voltage/polarity (normal for MOS caps and junctions) - use the C(V) data instead.")
    elif good:
        c = a.current / good[0]["a"]
        print(f" C                      : {fmt_f(c)}   (from dV/dt at 0 V, +I only)")
    else:
        c = np.nan
    if good and np.isfinite(c):
        b_avg = np.mean([r["b"] for r in good])
        a_avg = np.mean([abs(r["a"]) for r in good])
        rel = b_avg * a.vmax / a_avg          # relative change of dV/dt across the full voltage range
        r_detect = 100 * a.vmax / a.current   # leakage above this is too small to see
        if b_avg < 0 and -1 / (b_avg * c) < r_detect:
            print(f" Parallel leakage R     : {-1 / (b_avg * c):.3g} Ω  (from how dV/dt drops as V rises)*")
        else:
            print(f" Parallel leakage R     : not detectable (> {r_detect:.1e} Ω for these settings)")
        if rel > 0.1:
            print(" ! dV/dt rises with V: the capacitance falls with voltage (non-linear device, e.g.")
            print("   junction or MOS depletion). The single C value is at 0 V; see the C(V) file.")
        if max(r["scatter"] for r in good) > 0.1:
            print(" ! dV/dt does not follow a straight line vs V: non-linear device - see the C(V) file.")
        if b_avg < 0 and -1 / (b_avg * c) < r_detect:
            print("   *a capacitance that RISES with V looks the same as leakage. To tell them apart, run")
            print("    again at 2-3x the current: a real leakage R stays the same, a C(V) effect does not.")
    for r in runs:
        if not r["clamped"] and (np.ptp(r["v"][r["ok"]]) if r["ok"].any() else 0) < 0.05 * a.vmax:
            print(" ! Voltage barely moved (<5 % of vmax): C is large or leakage ~ I -> raise --current.")
            break
    print(" Remember: subtract the stray capacitance measured with the probes lifted.")
    print("=" * 78)

    if a.save_csv:
        os.makedirs(os.path.dirname(os.path.abspath(a.save_csv)), exist_ok=True)
        with open(a.save_csv, "w") as f:
            f.write(f"# I={a.current:.6E} A, vmax={a.vmax} V, interval={a.interval} s\n")
            f.write("direction,time_s,voltage_V,status,used_in_fit\n")
            for r in runs:
                for tk, vk, sk, ok in zip(r["t"], r["v"], r["st"], r["ok"]):
                    f.write(f"{'+I' if r['sign'] > 0 else '-I'},{tk:.3f},{vk:.6E},{sk},{int(ok)}\n")
        print(f"Raw data saved to {os.path.abspath(a.save_csv)}")
        # quasi-static C(V) from each run
        cv_path = os.path.splitext(a.save_csv)[0] + "_CV.csv"
        with open(cv_path, "w") as f:
            f.write("direction,voltage_V,capacitance_F\n")
            for r in runs:
                vv, cc = local_cv(r["t"], r["v"], r["ok"], r["sign"] * a.current)
                for x, y in zip(vv, cc):
                    f.write(f"{'+I' if r['sign'] > 0 else '-I'},{x:.6E},{y:.6E}\n")
        print(f"C(V) data saved to {os.path.abspath(cv_path)}")


# ----------------------------------------------------------------------------- mock
class MockResource:
    """Simulates the 4145B's responses for a capacitor with parallel leakage."""

    def __init__(self, c, r_leak, stray):
        self.c, self.r, self.cs = c + stray, r_leak, stray
        self.vc = 0.0
        self.mode = self.val = self.comp = None
        self.wt = self.iv = self.nr = 0
        self.data = None
        self.rng = np.random.default_rng(1)

    def write(self, cmd):
        c = cmd.strip()
        m = re.match(r"CH(\d), 'V\d', 'I\d', (\d), 3", c)
        if m and m.group(2) in ("1", "2"):      # the forcing SMU (the other one is COM = 3)
            self.mode = int(m.group(2))
        if m2 := re.match(r"[VI]C\d, ([^,]+), ([^,]+)", c):
            self.val, self.comp = float(m2.group(1)), float(m2.group(2))
        for code, attr in (("WT", "wt"), ("IN", "iv"), ("NR", "nr")):
            if c.startswith(code + " "):
                setattr(self, attr, float(c[3:]))
        if c == "ME1":
            self._run()

    def _run(self):
        dt = 1e-3
        n = int(self.nr)
        times = self.wt + self.iv * np.arange(n)
        out, t, k = [], 0.0, 0
        while k < n:
            if t >= times[k] - 1e-12:
                if self.mode == 2:
                    clamp = abs(self.vc) >= self.comp * 0.999
                    out.append(("C" if clamp else "N", self.vc + self.rng.normal(0, 2e-4)))
                else:
                    out.append(("N", -self.vc / 1e3 + self.rng.normal(0, 1e-12)))
                k += 1
            if self.mode == 2:
                self.vc += (self.val - self.vc / self.r) / self.c * dt
                self.vc = float(np.clip(self.vc, -self.comp, self.comp))
            else:
                self.vc *= np.exp(-dt / (1e3 * self.c))  # discharge through ~1 kOhm
            t += dt
        self.data = out
        self.ready = True

    def read_stb(self):
        r = getattr(self, "ready", False)
        self.ready = False if r else r
        return 1 if r else 0

    def query(self, cmd):
        return ",".join(f"{s}{v:+.4E}" for s, v in self.data) + "\r\n"

    def close(self):
        pass


if __name__ == "__main__":
    main()
