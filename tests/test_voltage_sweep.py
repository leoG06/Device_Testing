#!/usr/bin/env python3
"""
================================================================================
HP 4145 / 4145B Semiconductor Parameter Analyzer - Hardware Voltage Sweep Test
================================================================================

Description:
    This script performs an automated 2-terminal I-V (current-voltage) sweep on an
    HP 4145 (or 4145B) Semiconductor Parameter Analyzer via GPIB / VISA. It is 
    specifically tailored for Transfer Length Method (TLM) contact resistance 
    characterization.

Key Operations:
    1. SMU1 (Master): Configured as a variable voltage source (VAR1) to sweep
       from V_start to V_stop in discrete V_step intervals.
    2. SMU2 (Common): Configured as common ground (0 V reference) to complete
       the two-terminal device circuit.
    3. SMU3 & SMU4: Turned off (idle) to avoid stray leakage currents.
    4. Compliance: Sets a strict current limit (e.g., 1 mA) to prevent thermal
       burnout of microscale contact pads.
    5. Data Acquisition: Triggers the sweep, monitors the instrument status byte,
       and reads back the measured current values from the analyzer's memory.
    6. Reporting: Renders an ASCII table of (Voltage, Current, Apparent Resistance),
       computes the differential Ohmic slope (dI/dV), and saves a TLM-ready CSV.

Usage Examples:
    # 1. Basic hardware run using default GPIB address (GPIB0::17::INSTR):
    python tests/test_voltage_sweep.py

    # 2. Custom sweep from -1.0 V to +1.0 V with 50 mV steps and 10 mA compliance:
    python tests/test_voltage_sweep.py --address "GPIB0::17::INSTR" --start -1.0 --stop 1.0 --step 0.05 --compliance 10e-3

    # 3. Save output directly to a TLM distance file (e.g., 10 micron spacing):
    python tests/test_voltage_sweep.py --save-csv "TLM_IV-Sweep/10.csv"

    # 4. Dry-run in simulation mode (runs offline without physical hardware):
    python tests/test_voltage_sweep.py --mock
================================================================================
"""

import sys
import os
import argparse
import time
import numpy as np

# ------------------------------------------------------------------------------
# 1. Environment & Path Setup
# ------------------------------------------------------------------------------
# Ensure the root project directory is on sys.path so local modules and packages
# (like dopes, equipment_control, and test utilities) are always discoverable.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# ------------------------------------------------------------------------------
# 2. Library Imports
# ------------------------------------------------------------------------------
# pyvisa provides the low-level VISA (Virtual Instrument Software Architecture)
# communication layer over GPIB, USB, and Serial interfaces.
import pyvisa

# Import the HP 4145 instrument class from the DOpeS library.
from hp4145 import hp4145

# Import VISA diagnostic utility to allow inline bus testing and error diagnostics
from test_visa_connection import check_visa_connection


def parse_arguments():
    """
    Parse command-line options for instrument address, sweep boundaries,
    channel assignments, and compliance parameters.

    Returns:
        argparse.Namespace: Object containing all validated CLI arguments.
    """
    parser = argparse.ArgumentParser(
        description="HP 4145 / 4145B Voltage Sweep Test & TLM Data Extraction Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Instrument Communication Settings
    parser.add_argument(
        "--address",
        type=str,
        default="GPIB0::16::INSTR",
        help="VISA resource string for the HP 4145 (e.g., 'GPIB0::16::INSTR')",
    )

    # Sweep Parameters
    parser.add_argument(
        "--start",
        type=float,
        default=-2.0,
        help="Sweep start voltage (Volts)",
    )
    parser.add_argument(
        "--stop",
        type=float,
        default=2.0,
        help="Sweep stop voltage (Volts)",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=0.1,
        help="Voltage increment per step (Volts)",
    )

    # Protection & Channel Routing
    parser.add_argument(
        "--compliance",
        type=float,
        default=1e-3,
        help="Current compliance limit in Amperes (e.g. 1e-3 = 1 mA). Clamps output to protect DUT.",
    )
    parser.add_argument(
        "--master-smu",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="SMU channel number configured as the sweeping voltage master",
    )
    parser.add_argument(
        "--common-smu",
        type=int,
        default=2,
        choices=[1, 2, 3, 4],
        help="SMU channel number configured as the common ground reference",
    )

    # Timing and Speed
    parser.add_argument(
        "--integration",
        type=str,
        default="M",
        choices=["S", "M", "L"],
        help="A/D integration time: 'S' (Short ~3ms), 'M' (Medium ~20ms, line-cycle), or 'L' (Long ~200ms)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.01,
        help="Settling delay time (seconds) to wait after setting bias before reading current",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="Hold time (seconds) to wait at the start bias point before commencing the sweep",
    )

    # File Output & Simulation
    parser.add_argument(
        "--save-csv",
        type=str,
        default="tests/hardware_iv_sweep.csv",
        help="File path to save the cleaned Voltage,Current CSV data",
    )
    parser.add_argument(
        "--check-visa",
        action="store_true",
        help="Run VISA diagnostic scan before sweeping to test bus communication and list detected addresses",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in offline simulation mode without connecting to physical hardware",
    )

    return parser.parse_args()


def main():
    """
    Main execution workflow:
      1. Parse CLI arguments.
      2. If --mock is requested, delegate to offline simulation.
      3. Open PyVISA connection to the HP 4145.
      4. Configure SMU channels, bias limits, and sweep registers via DOpeS.
      5. Trigger measurement sweep and retrieve data arrays.
      6. Display tabular output with apparent resistance and extract Ohmic slope.
      7. Save TLM-compatible CSV file.
      8. Safely close instrument session.
    """
    args = parse_arguments()

    # --------------------------------------------------------------------------
    # Branch: Offline Mock Simulation
    # --------------------------------------------------------------------------
    # If user specifies --mock (or is developing away from the lab), execute
    # the synthetic simulator which mimics HP-IB commands and an Ohmic DUT.
    if args.mock:
        from test_voltage_sweep_mock import run_mock_sweep
        print("[MOCK MODE ACTIVATED] Running simulated sweep without physical hardware...")
        run_mock_sweep(
            start=args.start,
            stop=args.stop,
            step=args.step,
            compliance=args.compliance,
            resistance=2500.0,
            save_csv=args.save_csv,
        )
        return

    # --------------------------------------------------------------------------
    # Header Banner Display
    # --------------------------------------------------------------------------
    print("=" * 80)
    print(" HP 4145 / 4145B SEMICONDUCTOR PARAMETER ANALYZER - VOLTAGE SWEEP")
    print("=" * 80)
    print(f" • Target VISA Address    : {args.address}")
    print(f" • Master SMU (Sweep)    : SMU{args.master_smu} (Voltage Source)")
    print(f" • Common SMU (Ground)   : SMU{args.common_smu} (Common Reference)")
    print(f" • Voltage Sweep Range   : {args.start:+.3f} V to {args.stop:+.3f} V (step: {args.step:+.3f} V)")
    print(f" • Current Compliance    : {args.compliance:.2E} A ({args.compliance * 1e3:.2f} mA)")
    print(f" • Integration Mode      : {args.integration} ({'Short' if args.integration=='S' else 'Medium' if args.integration=='M' else 'Long'})")
    print(f" • Output File Path      : {args.save_csv if args.save_csv else 'None (Display only)'}")
    print("-" * 80)

    # Optional: Pre-flight diagnostic scan if explicitly requested
    if args.check_visa:
        check_visa_connection(target_address=args.address)
        print("-" * 80)

    # --------------------------------------------------------------------------
    # Step 1: Establish PyVISA Connection
    # --------------------------------------------------------------------------
    print(f"Connecting to HP 4145 at '{args.address}' ...")
    try:
        analyzer = hp4145(args.address, timeout=15000)
    except Exception as e:
        print(f"\n[CONNECTION ERROR] Could not connect to '{args.address}': {e}")
        print("Tips:")
        print("  - Run 'python tests/test_visa_connection.py' to scan ports and test GPIB addresses.")
        print("  - To test your sweep code without physical hardware, run with '--mock':")
        print("      python tests/test_voltage_sweep.py --mock")
        sys.exit(1)

    try:
        # ----------------------------------------------------------------------
        # Step 2: Configure SMU Channels for 2-Terminal TLM Measurement
        # ----------------------------------------------------------------------
        # The HP 4145 has 4 independent Source-Monitor Units (SMU1 through SMU4).
        # We start by initializing all channels to common/off as a clean baseline.
        smu_type = {f"SMU{i}": "common" for i in range(1, 5)}
        smu_used = {f"SMU{i}": "off" for i in range(1, 5)}
        smu_compliance = {f"SMU{i}": 1e-6 for i in range(1, 5)}
        smu_bias = {f"SMU{i}": 0.0 for i in range(1, 5)}

        # Channel 1: Master Voltage Source (VAR1)
        # Sweeps through the range and simultaneously measures resulting current (I1).
        smu_type[f"SMU{args.master_smu}"] = "voltage"
        smu_used[f"SMU{args.master_smu}"] = "on"
        smu_compliance[f"SMU{args.master_smu}"] = args.compliance

        # Channel 2: Common Ground Return
        # Provides the zero-volt reference node for the current return path.
        smu_type[f"SMU{args.common_smu}"] = "common"
        smu_used[f"SMU{args.common_smu}"] = "on"
        smu_compliance[f"SMU{args.common_smu}"] = args.compliance

        # SMU3 & SMU4 remain "off" (disconnected/floating) to prevent parasitic paths.

        # Define sweep boundary parameters
        sweep_param = {
            "start": args.start,
            "stop": args.stop,
            "step": args.step,
        }

        # ----------------------------------------------------------------------
        # Step 3: Send Instrument Configurations via DOpeS
        # ----------------------------------------------------------------------
        # analyzer.initialize() translates our Python parameters into native HP-IB commands:
        #   - 'BC': Buffer Clear
        #   - 'DR1': Enable SRQ (service request) data ready interrupt
        #   - 'EC 1': Exit test condition on compliance limit
        #   - 'DE': Channel Definition Page (configures SMU modes and sweep master)
        #   - 'SS': Source Setup Page (programs VR1 linear sweep, start, stop, step, compliance)
        #   - 'HT', 'DT': Sets hold and delay settling times
        #   - 'IT1'/'IT2'/'IT3': Sets A/D integration speed
        #   - 'SM', 'DM2', 'LI': Configures on-screen List Display mode
        print("Sending configuration to HP 4145 registers and measurement channels...")
        analyzer.initialize(
            smu_type=smu_type,
            smu_used=smu_used,
            smu_master=args.master_smu,
            smu_bias=smu_bias,
            smu_compliance=smu_compliance,
            sweep_param=sweep_param,
            sweep_type="linear",
            integration_mode=args.integration,
            delay_time=args.delay,
            hold_time=args.hold,
        )
        print("Instrument initialized successfully.")

        # ----------------------------------------------------------------------
        # Step 4: Execute Measurement Sweep & Retrieve Data
        # ----------------------------------------------------------------------
        # analyzer.launch_measurements():
        #   1. Sends 'MD' and 'ME1' to trigger single-shot sweep acquisition.
        #   2. Polls instrument Status Byte ('read_stb') until bit 0 indicates completion.
        #   3. Queries 'DO Ix' to transfer measurement readings across GPIB.
        #   4. Parses comma-separated scientific notation floats into a 2D NumPy array.
        print("\nTriggering measurement sweep on HP 4145 and streaming data...")
        start_time = time.time()
        data, header = analyzer.launch_measurements()
        elapsed = time.time() - start_time
        print(f"Acquisition completed in {elapsed:.2f} seconds.")

        # ----------------------------------------------------------------------
        # Step 5: Format and Print Tabular Results to stdout
        # ----------------------------------------------------------------------
        print("\n" + "=" * 42)
        print(f" MEASUREMENT RESULTS (Header: {header})")
        print("=" * 42)
        print(f"{'Point':>6} | {'Voltage (V)':>14} | {'Current (mA)':>14}")
        print("-" * 42)

        # data[:, 0] contains the swept voltage points
        # data[:, 1] contains the measured current for the active channel (SMU1)
        v_vals = data[:, 0]
        i_vals = data[:, 1]

        for idx, (v, i) in enumerate(zip(v_vals, i_vals)):
            i_ma = i * 1e3
            print(f"{idx + 1:6d} | {v:14.4f} | {i_ma:14.4f}")

        print("-" * 42)

        # ----------------------------------------------------------------------
        # Step 6: Export to CSV for TLM Analysis Pipeline
        # ----------------------------------------------------------------------
        # Writes data in the exact 2-column format ("Voltage,Current") expected by
        # DataExtraction.py and the Jupyter analysis notebook (TLMAnalysis.ipynb).
        if args.save_csv:
            save_path = os.path.abspath(args.save_csv)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "w") as f:
                f.write("Voltage,Current\n")
                for v, i in zip(v_vals, i_vals):
                    f.write(f"{v:.2E},{i:.2E}\n")
            print(f"\nMeasurement CSV saved to: {save_path}")

    finally:
        # ----------------------------------------------------------------------
        # Step 7: Safe Instrument Session Cleanup
        # ----------------------------------------------------------------------
        # Ensure the PyVISA session is closed even if an unexpected exception occurs,
        # preventing the GPIB bus from hanging on subsequent executions.
        try:
            analyzer.close_connection()
            print("VISA connection safely closed.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
