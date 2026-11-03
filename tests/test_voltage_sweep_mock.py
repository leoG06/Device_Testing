#!/usr/bin/env python3
"""
Mock / Simulation Test Script for HP4145 Voltage Sweep.

This script tests the HP4145 voltage sweep workflow without requiring physical
hardware or a connected GPIB bus. It simulates the HP4145B GPIB command set,
mimics an ohmic contact / resistor (I = V / R), respects current compliance,
and prints the resulting tabular data and summary statistics.

Usage:
    python tests/test_voltage_sweep_mock.py
    python tests/test_voltage_sweep_mock.py --start -3.0 --stop 3.0 --step 0.25 --resistance 1500
"""

import sys
import os
import argparse
import numpy as np

# Ensure project root and tests directory are on PYTHONPATH
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from dopes.equipment_control.hp4145 import hp4145


class MockPyVISAResource:
    """
    Simulates a PyVISA MessageBasedResource connected to an HP 4145B.
    Parses HP 4145 HP-IB commands and returns synthetic measurement responses.
    """

    def __init__(self, simulated_resistance=2000.0, noise_std=1e-7):
        self.simulated_resistance = float(simulated_resistance)  # Ohms (Ohmic DUT)
        self.noise_std = float(noise_std)
        self.timeout = 10000
        self.write_termination = "\r\n"
        self.read_termination = "\r\n"
        self.send_end = True

        # Internal state
        self.command_log = []
        self.master_smu = 1
        self.sweep_start = 0.0
        self.sweep_stop = 0.0
        self.sweep_step = 0.1
        self.compliance = 1e-3  # 1 mA default
        self.status_byte = 1    # Ready

    def write(self, command):
        """Record and parse instrument commands."""
        self.command_log.append(command.strip())
        cmd = command.strip()

        # Parse VR1 (Voltage linear sweep VAR1): VR1, start, stop, step, compliance
        if cmd.startswith("VR1"):
            parts = [p.strip() for p in cmd[3:].lstrip(",").split(",")]
            if len(parts) >= 4:
                self.sweep_start = float(parts[0])
                self.sweep_stop = float(parts[1])
                self.sweep_step = float(parts[2])
                self.compliance = float(parts[3])

        elif cmd.startswith("CH"):
            # Channel assignment, e.g., CH1, 'V1', 'I1', 1, 1
            pass

    def query(self, command, delay=0):
        """Simulate instrument query responses."""
        self.command_log.append(f"QUERY: {command.strip()}")
        cmd = command.strip()

        # HP 4145 Data Output command DO 'Ix'
        if cmd.startswith("DO"):
            # Compute current array based on current sweep parameters
            v_start = self.sweep_start
            v_stop = self.sweep_stop
            v_step = self.sweep_step
            if v_step == 0:
                v_arr = np.array([v_start])
            else:
                if v_stop > v_start and v_step < 0:
                    v_step = abs(v_step)
                elif v_stop < v_start and v_step > 0:
                    v_step = -abs(v_step)
                v_arr = np.arange(v_start, v_stop + v_step / 2.0, v_step)

            # I = V / R with compliance limiting and slight noise
            noise = np.random.normal(0, self.noise_std, size=len(v_arr))
            i_ideal = (v_arr / self.simulated_resistance) + noise
            i_clamped = np.clip(i_ideal, -self.compliance, self.compliance)

            # Format like HP 4145: comma-separated scientific notation floats with status prefix
            # e.g., "N+1.000000E-03,N+2.000000E-03"
            readings = [f"N{val:+.6E}" for val in i_clamped]
            return ",".join(readings)

        elif cmd == "*IDN?":
            return "HEWLETT-PACKARD,4145B,0,REV_2.4"

        return "0.000000E+00"

    def read_stb(self):
        """Return status byte (bit 0 = 1 for data ready)."""
        return self.status_byte

    def close(self):
        pass


class MockResourceManager:
    """Mock PyVISA ResourceManager providing simulated instruments."""

    def __init__(self, resource):
        self.resource = resource

    def open_resource(self, address):
        return self.resource

    def list_resources(self):
        return ("GPIB0::17::INSTR", "ASRL1::INSTR")


def run_mock_sweep(start=-2.0, stop=2.0, step=0.2, compliance=1e-3, resistance=2500.0, save_csv=None):
    """
    Execute a simulated voltage sweep on the HP4145 analyzer.
    """
    print("=" * 78)
    print(" HP 4145B SEMICONDUCTOR PARAMETER ANALYZER - VOLTAGE SWEEP (SIMULATION)")
    print("=" * 78)
    print(f" • Mode: Offline Simulation (Mock DUT)")
    print(f" • Simulated Contact / DUT Resistance: {resistance:,.1f} Ω")
    print(f" • Sweep Range: {start:+.3f} V to {stop:+.3f} V (step: {step:+.3f} V)")
    print(f" • Current Compliance Limit: {compliance:.2E} A ({compliance * 1e3:.2f} mA)")
    print("-" * 78)

    # Instantiate mock instrument and manager
    mock_resource = MockPyVISAResource(simulated_resistance=resistance)
    mock_rm = MockResourceManager(mock_resource)

    # Initialize HP4145 class
    analyzer = hp4145("GPIB0::17::INSTR", rm=mock_rm)

    # Setup for 2-terminal TLM I-V Sweep:
    # SMU1 = Voltage sweep master
    # SMU2 = Common (GND)
    # SMU3, SMU4 = Disabled
    smu_type = {"SMU1": "voltage", "SMU2": "common", "SMU3": "common", "SMU4": "common"}
    smu_used = {"SMU1": "on", "SMU2": "on", "SMU3": "off", "SMU4": "off"}
    smu_compliance = {"SMU1": compliance, "SMU2": compliance, "SMU3": 1e-6, "SMU4": 1e-6}
    sweep_param = {"start": start, "stop": stop, "step": step}

    print("Sending configuration to HP 4145...")
    analyzer.initialize(
        smu_type=smu_type,
        smu_used=smu_used,
        smu_master=1,
        smu_compliance=smu_compliance,
        sweep_param=sweep_param,
        sweep_type="linear",
        integration_mode="M",  # Medium integration time
        delay_time=0.01,
        hold_time=0.0,
    )
    print(f"Configuration complete ({len(mock_resource.command_log)} GPIB commands executed).")

    print("\nExecuting voltage sweep and acquiring data...")
    data, header = analyzer.launch_measurements()

    print(f"\nSweep Completed Successfully!")
    print(f"Data shape: {data.shape[0]} points × {data.shape[1]} columns")
    print(f"Raw Header: {header}")
    print("-" * 78)

    # Print formatted table
    print(f"{'Point':>6} | {'Voltage (V)':>14} | {'Current (A)':>16} | {'Current (mA)':>14} | {'Apparent R (Ω)':>16}")
    print("-" * 78)

    v_vals = data[:, 0]
    i_vals = data[:, 1]

    for idx, (v, i) in enumerate(zip(v_vals, i_vals)):
        i_ma = i * 1e3
        # Calculate apparent resistance avoiding division by zero
        r_app = abs(v / i) if abs(i) > 1e-12 else float("nan")
        r_str = f"{r_app:16.2f}" if not np.isnan(r_app) else f"{'N/A (V≈0)':>16}"
        print(f"{idx + 1:6d} | {v:14.4f} | {i:16.6E} | {i_ma:14.4f} | {r_str}")

    print("-" * 78)

    # Summary Statistics
    print("\n" + "=" * 32 + " SWEEP SUMMARY " + "=" * 32)
    print(f" • Total Measurement Points : {len(v_vals)}")
    print(f" • Voltage Sweep Range      : [{v_vals[0]:+.4f} V, {v_vals[-1]:+.4f} V]")
    print(f" • Current Range Measured   : [{i_vals.min():+.6E} A, {i_vals.max():+.6E} A]")
    
    # Linear regression to extract differential resistance (Ohmic slope)
    # Exclude saturated compliance points if any
    linear_mask = (abs(i_vals) < 0.98 * compliance)
    if np.sum(linear_mask) >= 2:
        v_fit = v_vals[linear_mask]
        i_fit = i_vals[linear_mask]
        slope, intercept = np.polyfit(v_fit, i_fit, 1)  # I = slope * V + intercept
        extracted_r = 1.0 / slope if slope != 0 else float("inf")
        print(f" • Extracted Ohmic Slope dI/dV: {slope:.6E} S (Siemens)")
        print(f" • Extracted Resistance (1/slope): {extracted_r:,.2f} Ω (Target DUT: {resistance:,.1f} Ω)")
        error_pct = abs(extracted_r - resistance) / resistance * 100.0
        print(f" • Extraction Accuracy      : {100.0 - error_pct:.3f}% (Sim error: {error_pct:.3f}%)")
    
    compliance_hit = np.any(np.isclose(abs(i_vals), compliance, atol=1e-8))
    print(f" • Compliance Reached       : {'YES (Current Clamped)' if compliance_hit else 'NO (Linear Region Only)'}")
    print("=" * 78)

    # Save to CSV if requested
    if save_csv:
        save_path = os.path.abspath(save_csv)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        # Format matching TLM_IV-Sweep: Voltage,Current
        with open(save_path, "w") as f:
            f.write("Voltage,Current\n")
            for v, i in zip(v_vals, i_vals):
                f.write(f"{v:.2E},{i:.2E}\n")
        print(f"Saved TLM-compatible data file to: {save_path}")

    return data, header


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HP4145 Voltage Sweep Offline Simulation Test")
    parser.add_argument("--start", type=float, default=-2.0, help="Sweep start voltage in Volts (default: -2.0)")
    parser.add_argument("--stop", type=float, default=2.0, help="Sweep stop voltage in Volts (default: 2.0)")
    parser.add_argument("--step", type=float, default=0.2, help="Sweep step voltage in Volts (default: 0.2)")
    parser.add_argument("--compliance", type=float, default=1e-3, help="Current compliance in Amperes (default: 1e-3)")
    parser.add_argument("--resistance", type=float, default=2500.0, help="Simulated contact resistance in Ohms (default: 2500)")
    parser.add_argument("--save-csv", type=str, default="tests/mock_iv_sweep.csv", help="Path to save output CSV")

    args = parser.parse_args()

    run_mock_sweep(
        start=args.start,
        stop=args.stop,
        step=args.step,
        compliance=args.compliance,
        resistance=args.resistance,
        save_csv=args.save_csv,
    )

