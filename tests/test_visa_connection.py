#!/usr/bin/env python3
"""
VISA & GPIB Connection Diagnostic Utility.

Scans the system for connected VISA instruments, verifies communication backends
(NI-VISA, pyvisa-py, gpib-ctypes), and attempts identification queries (*IDN? or STB).

Usage:
    python tests/test_visa_connection.py
    python tests/test_visa_connection.py --query "GPIB0::17::INSTR"
"""

import argparse
import pyvisa


def check_visa_connection(target_address=None):
    """
    Scan the system for VISA instruments and test communication with a target device.
    """
    print("=" * 78)
    print(" VISA & GPIB HARDWARE DIAGNOSTIC UTILITY")
    print("=" * 78)

    # 1. Inspect PyVISA Backends
    try:
        rm = pyvisa.ResourceManager()
        print(f" • PyVISA Resource Manager Backend: {rm}")
    except Exception as e:
        print(f" [ERROR] Could not initialize PyVISA ResourceManager: {e}")
        return

    # 2. List all detected resources
    print("\nScanning for connected instruments across all interfaces...")
    try:
        resources = rm.list_resources()
        if resources:
            print(f"Found {len(resources)} VISA resource(s):")
            for idx, res in enumerate(resources, 1):
                print(f"   [{idx}] {res}")
        else:
            print(" No VISA resources found.")
            print("\n  Troubleshooting Tips:")
            print("   - Ensure the HP 4145 is powered on.")
            print("   - Ensure the GPIB-USB adapter (e.g. NI GPIB-USB-HS) is plugged in.")
            print("   - Check that drivers (NI-488.2, NI-VISA, or linux-gpib) are installed.")
            print("   - Note: on macOS without NI-VISA kernel drivers, direct GPIB-USB may require")
            print("     a USB-serial adapter, a GPIB bridge, or running on Linux/Windows.")
    except Exception as e:
        print(f" [WARNING] Error scanning resources: {e}")
        resources = ()

    # 3. Test communication with target address if provided
    addr = target_address or ("GPIB0::16::INSTR" if "GPIB0::16::INSTR" in resources else None)
    if addr:
        print("\n" + "-" * 78)
        print(f"Testing direct communication with target: {addr} ...")
        try:
            inst = rm.open_resource(addr)
            inst.timeout = 5000
            print(f" Successfully opened VISA session to {addr}")

            # Try query *IDN?
            try:
                idn = inst.query("*IDN?")
                print(f" Identification (*IDN?): {idn.strip()}")
            except Exception:
                print(" Device did not respond to standard SCPI *IDN? (Normal for older HP 4145).")

            # Try read status byte
            try:
                stb = inst.read_stb()
                print(f" Status Byte (STB): {stb} (binary: {bin(stb)})")
            except Exception as e:
                print(f" Could not read STB: {e}")

            inst.close()
            print(" Instrument session closed cleanly.")
        except Exception as e:
            print(f" Failed to communicate with {addr}: {e}")

    print("=" * 78)


# Alias for backwards compatibility
test_connection = check_visa_connection


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VISA Instrument Connection Diagnostics")
    parser.add_argument("--query", type=str, default=None, help="Specific VISA resource address to test")
    args = parser.parse_args()
    check_visa_connection(args.query)
    