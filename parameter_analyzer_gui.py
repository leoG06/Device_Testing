#!/usr/bin/env python3
"""HP 4145/4145B Parameter Analyzer GUI.

Requires PyQt5, matplotlib, numpy, and the project's existing PyVISA setup.

Run:
    python parameter_analyzer_gui.py

Stop takes effect between curves, not during the driver's blocking acquisition.
Closing a VISA session does not guarantee that instrument outputs are off.
"""

import argparse
import csv
import datetime
import glob
import io
import math
import os
import re
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout

# ------------------------------------------------------------------------------
# Path & environment bootstrap
# ------------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")
CSV_DIR = os.path.join(PROJECT_ROOT, "CSV")
for path in (PROJECT_ROOT, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)
# Append (rather than prepend) .venv packages so Anaconda's compiled C-extensions
# (e.g., numpy/matplotlib) take precedence while pure-Python VISA libs remain available.
for venv_site in glob.glob(os.path.join(PROJECT_ROOT, ".venv", "lib", "python*", "site-packages")):
    if venv_site not in sys.path:
        sys.path.append(venv_site)

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError as exc:
    # Fall back to Anaconda Python if launched from a venv without PyQt5.
    anaconda_py = "/opt/anaconda3/bin/python3"
    if (
        __name__ == "__main__"
        and os.path.exists(anaconda_py)
        and os.path.abspath(sys.executable) != os.path.abspath(anaconda_py)
    ):
        os.execv(anaconda_py, [anaconda_py, os.path.abspath(__file__)] + sys.argv[1:])
    raise RuntimeError(
        "PyQt5 is required. Run this file in a Python environment containing "
        "PyQt5, matplotlib, numpy, pyvisa, and your GPIB backend."
    ) from exc

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT

import parameter_analyzer_sweep as ts
from tests.hp4145 import hp4145


# ------------------------------------------------------------------------------
# UI limits, SMU role definitions, and SI unit parsing
# ------------------------------------------------------------------------------
MAX_HISTORY = 60
MAX_LOG_BLOCKS = 10000
MAX_OFFSETS = 50
MAX_POINTS = 512

ROLES = [
    ("off", "Off — disconnected"),
    ("com", "Common — 0 V return"),
    ("sweep-v", "Sweep voltage"),
    ("sweep-i", "Sweep current"),
    ("const-v", "Constant voltage"),
    ("const-i", "Constant current"),
    ("step-v", "Stepped voltage"),
    ("step-i", "Stepped current"),
]

SI_FACTORS = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9,
    "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "M": 1e6, "G": 1e9,
}


def number(text, label, optional=False):
    """Parse a float with optional SI suffix (e.g., '10m', '1u', '50n') and validate finiteness."""
    text = text.strip()
    if not text:
        if optional:
            return None
        raise ValueError(f"{label} is required")
    try:
        value = float(text)
    except ValueError:
        match = re.fullmatch(
            r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
            r"\s*([fpnuµmkMG])(?:[AVs])?",
            text,
        )
        if not match:
            raise ValueError(f"{label}: enter a number such as 0.05 or 10u")
        value = float(match.group(1)) * SI_FACTORS[match.group(2)]
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def output_path(text):
    """Resolve relative output paths into the project's CSV/ directory."""
    text = text.strip()
    if os.path.isabs(text):
        return os.path.abspath(text)
    norm = os.path.normpath(text)
    parts = norm.split(os.sep)
    while parts and parts[0] in (".", "CSV", "tests"):
        parts = parts[1:]
    rel = os.path.join(*parts) if parts else "hardware_sweep.csv"
    return os.path.abspath(os.path.join(CSV_DIR, rel))


def scale(values, unit):
    """Return an SI scaling factor and unit label ignoring NaN/Inf values."""
    values = np.asarray(values)
    finite = values[np.isfinite(values)]
    return ts.si_scale(finite, unit) if finite.size else (1.0, unit)


def preset(roles, start, stop, step, comp, **extra):
    """Build a preset configuration dictionary with sensible defaults."""
    result = dict(
        roles=roles, start=start, stop=stop, step=step,
        comp=comp, sweep_type="linear", integration="M",
        delay="0.01", hold="0", multi=False,
    )
    result.update(extra)
    return result


PRESETS = {
    "Custom": None,
    "1. Two-terminal I–V": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("off", "", ""), ("off", "", "")],
        "-1", "1", "0.05", "10m",
    ),
    "2. MOSFET output curve": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("const-v", "1.5", "1u"), ("off", "", "")],
        "0", "3", "0.05", "20m",
    ),
    "3. MOSFET transfer curve": preset(
        [("const-v", "0.1", "20m"), ("com", "", ""),
         ("sweep-v", "", ""), ("off", "", "")],
        "-1", "3", "0.05", "1u",
    ),
    "4. BJT output curve": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("const-i", "10u", "2"), ("off", "", "")],
        "0", "5", "0.1", "50m",
    ),
    "5. Diode logarithmic current sweep": preset(
        [("sweep-i", "", ""), ("com", "", ""),
         ("off", "", ""), ("off", "", "")],
        "1n", "10m", "0.1", "2", sweep_type="log",
    ),
    "6. Offline three-SMU demo": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("const-v", "0.5", "1m"), ("off", "", "")],
        "-2", "2", "0.1", "1m", mock=True,
    ),
    "7. MOSFET output family": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("step-v", "0.5", "1u"), ("off", "", "")],
        "0", "3", "0.05", "20m",
        multi=True, offset_smu=3, offset_mode="voltage",
        offset_start="0.5", offset_stop="2.5", offset_step="0.5",
        offset_list="0.5, 1, 1.5, 2, 2.5", offset_comp="1u",
    ),
    "8. MOSFET transfer family": preset(
        [("step-v", "0.1", "20m"), ("com", "", ""),
         ("sweep-v", "", ""), ("off", "", "")],
        "-1", "3", "0.05", "1u",
        multi=True, offset_smu=1, offset_mode="voltage",
        offset_start="0.1", offset_stop="2", offset_step="0.5",
        offset_list="0.1, 0.5, 1, 2", offset_comp="20m",
        offset_spec="list",
    ),
    "9. BJT output family": preset(
        [("sweep-v", "", ""), ("com", "", ""),
         ("step-i", "10u", "2"), ("off", "", "")],
        "0", "5", "0.1", "50m",
        multi=True, offset_smu=3, offset_mode="current",
        offset_start="10u", offset_stop="50u", offset_step="10u",
        offset_list="10u, 20u, 30u, 40u, 50u", offset_comp="2",
    ),
}


class SweepWorker(QtCore.QObject):
    """Background worker that executes one or more HP 4145B sweeps and writes CSV outputs."""

    log = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(str)
    curve = QtCore.pyqtSignal(object)
    completed = QtCore.pyqtSignal(str, bool)

    def __init__(self, jobs, offset_info, cancel_event):
        super().__init__()
        self.jobs = jobs
        self.offset_info = offset_info
        self.cancel_event = cancel_event

    @staticmethod
    def flush(file):
        """Force buffered writes to disk after each completed curve."""
        file.flush()
        os.fsync(file.fileno())

    def metadata(self, args, setup, info, completed, started):
        """Atomically write/update the companion _setup.txt summary file."""
        path = os.path.splitext(args.save_csv)[0] + "_setup.txt"
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            file.write(f"Started: {started}\n")
            file.write(f"VISA address: {args.address}\n")
            file.write(f"Simulator: {args.mock}\n")
            file.write(f"Completed curves: {completed}/{len(self.jobs)}\n")
            file.write(
                f"Sweep: {args.sweep_type}; start={setup['start']:.17g}; "
                f"stop={setup['stop']:.17g}; step={setup['step']:.17g}\n"
            )
            file.write(
                f"Integration: {args.integration}; "
                f"delay={args.delay:g} s; hold={args.hold:g} s\n"
            )
            for smu in ts.SMUS:
                file.write(
                    f"SMU{smu}: {getattr(args, f'smu{smu}')}\n"
                )
            if info:
                file.write(
                    f"Software-stepped SMU: {info['smu']}; "
                    f"mode={info['mode']}; offsets={info['values']!r}\n"
                )
            file.write(
                "Column 0 contains the driver's programmed sweep values, "
                "not a separately measured source voltage/current.\n"
            )
            self.flush(file)
        os.replace(temporary, path)

    @QtCore.pyqtSlot()
    def run(self):
        analyzer = None
        data_file = flags_file = None
        runs = []
        error = None
        started = datetime.datetime.now().isoformat(timespec="seconds")
        t0 = time.monotonic()
        args0, setup0, _, _ = self.jobs[0]
        expected_header = None
        writer = flag_writer = None

        try:
            if self.cancel_event.is_set():
                return

            # Open outputs before connecting, so an invalid output directory
            # is detected before any instrument measurement is launched.
            if args0.save_csv:
                os.makedirs(os.path.dirname(args0.save_csv), exist_ok=True)
                data_file = open(
                    args0.save_csv, "w", newline="", encoding="utf-8"
                )
                flags_path = (
                    os.path.splitext(args0.save_csv)[0] + "_flags.csv"
                )
                flags_file = open(
                    flags_path, "w", newline="", encoding="utf-8"
                )
                writer = csv.writer(data_file)
                flag_writer = csv.writer(flags_file)
                flag_writer.writerow([
                    "curve", "offset", "point", "sweep_value",
                    "channel", "flag", "meaning",
                ])
                self.flush(flags_file)
                self.metadata(args0, setup0, self.offset_info, 0, started)
                self.log.emit(f"Saving completed curves to {args0.save_csv}")

            if args0.mock:
                analyzer = hp4145.__new__(hp4145)
                analyzer.pyvisa_resource = ts.MockResource()
                self.log.emit("Offline simulator selected.")
            else:
                self.log.emit(f"Connecting to {args0.address}")
                analyzer = hp4145(args0.address, timeout=15000)

            for index, (args, setup, label, offset) in enumerate(
                self.jobs, start=1
            ):
                if self.cancel_event.is_set():
                    break

                self.progress.emit(
                    f"Acquiring curve {index}/{len(self.jobs)} — {label}"
                )
                analyzer.initialize(
                    smu_type=setup["smu_type"],
                    smu_used=setup["smu_used"],
                    smu_master=setup["master"],
                    smu_bias=setup["smu_bias"],
                    smu_compliance=setup["smu_comp"],
                    sweep_param={
                        "start": setup["start"],
                        "stop": setup["stop"],
                        "step": setup["step"],
                    },
                    sweep_type=args.sweep_type,
                    integration_mode=args.integration,
                    delay_time=args.delay,
                    hold_time=args.hold,
                )
                if self.cancel_event.is_set():
                    break

                # Compatible with the unmodified repository driver.
                data, header = analyzer.launch_measurements()
                data = np.array(data, dtype=float, copy=True)
                status = np.array(analyzer.last_status, copy=True)
                names = [name.strip() for name in header.split(",")]

                if (
                    data.ndim != 2
                    or data.shape[0] == 0
                    or data.shape[1] != len(names)
                    or status.shape != (data.shape[0], len(names) - 1)
                ):
                    raise ValueError("Instrument data/header/status mismatch")
                if expected_header is None:
                    expected_header = header
                    if writer:
                        columns = [
                            f"{names[0]}_set_{ts.unit_of(names[0])}"
                        ] + [
                            f"{name}_{ts.unit_of(name)}"
                            for name in names[1:]
                        ]
                        if self.offset_info:
                            unit = (
                                "V" if self.offset_info["mode"] == "voltage"
                                else "A"
                            )
                            columns.insert(
                                0, f"SMU{self.offset_info['smu']}_offset_{unit}"
                            )
                        writer.writerow(columns)
                elif header != expected_header:
                    raise ValueError("Channel header changed between curves")

                entry = (data, header, status, setup, args, label, offset)
                runs.append(entry)
                # Preserve data in the GUI even if a subsequent disk write fails.
                self.curve.emit(entry)

                if writer:
                    for row in data:
                        values = [f"{value:.12E}" for value in row]
                        if self.offset_info:
                            values.insert(0, f"{offset:.12E}")
                        writer.writerow(values)

                    for point in range(data.shape[0]):
                        for col, name in enumerate(names[1:]):
                            flag = str(status[point, col])
                            if flag != "N":
                                flag_writer.writerow([
                                    index, "" if offset is None else offset,
                                    point + 1, data[point, 0], name, flag,
                                    ts.STATUS_MEANING.get(flag, "unknown"),
                                ])
                    self.flush(data_file)
                    self.flush(flags_file)
                    self.metadata(
                        args0, setup0, self.offset_info, len(runs), started
                    )

                flagged = int(np.count_nonzero(status != "N"))
                self.log.emit(
                    f"Curve {index}: {len(data)} points, "
                    f"{flagged} flagged channel readings."
                )

            # Write a wide-format _family.csv (one column pair per stepped offset)
            # when all curves share an identical sweep axis.
            if args0.save_csv and self.offset_info and len(runs) > 1:
                reference = runs[0][0][:, 0]
                if all(
                    entry[0].shape == runs[0][0].shape
                    and np.array_equal(entry[0][:, 0], reference)
                    for entry in runs
                ):
                    path = (
                        os.path.splitext(args0.save_csv)[0] + "_family.csv"
                    )
                    names = [
                        name.strip() for name in runs[0][1].split(",")
                    ]
                    with open(
                        path, "w", newline="", encoding="utf-8"
                    ) as file:
                        family_writer = csv.writer(file)
                        columns = [
                            f"{names[0]}_set_{ts.unit_of(names[0])}"
                        ]
                        for entry in runs:
                            columns.extend(
                                f"{name}_{ts.unit_of(name)}_at_{entry[5]}"
                                for name in names[1:]
                            )
                        family_writer.writerow(columns)
                        for point, x in enumerate(reference):
                            row = [x]
                            for entry in runs:
                                row.extend(entry[0][point, 1:])
                            family_writer.writerow(
                                [f"{value:.12E}" for value in row]
                            )
                        self.flush(file)

        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.log.emit(traceback.format_exc())
        finally:
            for file in (data_file, flags_file):
                if file is not None:
                    try:
                        file.close()
                    except Exception as exc:
                        error = error or f"Output close failed: {exc}"
            if analyzer is not None:
                try:
                    analyzer.close_connection()
                    self.log.emit("VISA session closed.")
                except Exception as exc:
                    self.log.emit(f"VISA close failed: {exc}")
                    error = error or f"VISA close failed: {exc}"

            elapsed = time.monotonic() - t0
            if error:
                message = (
                    f"{error}. {len(runs)}/{len(self.jobs)} curves acquired; "
                    "check the log for saving errors."
                )
            else:
                state = "Stopped" if self.cancel_event.is_set() else "Completed"
                message = (
                    f"{state}: {len(runs)}/{len(self.jobs)} curves "
                    f"in {elapsed:.1f} s."
                )
            self.completed.emit(message, error is not None)


class MeasurementModel(QtCore.QAbstractTableModel):
    """Displays NumPy data without allocating a widget item for every cell."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = []
        self.locations = []
        self.headers = []
        self.factors = []
        self.multi = False

    def set_runs(self, runs):
        self.beginResetModel()
        try:
            self.runs = list(runs)
            self.locations = []
            self.headers = []
            self.factors = []
            if not runs:
                return
            names = [name.strip() for name in runs[0][1].split(",")]
            self.multi = any(entry[6] is not None for entry in runs)
            self.headers = ["Point"]
            if self.multi:
                self.headers.append("Offset")
            for col, name in enumerate(names):
                values = np.concatenate([entry[0][:, col] for entry in runs])
                factor, label = scale(values, ts.unit_of(name))
                self.factors.append(factor)
                self.headers.append(
                    f"{name}{' set' if col == 0 else ''} ({label})"
                )
            self.headers.append("Flags")
            self.locations = [
                (run_index, point)
                for run_index, entry in enumerate(runs)
                for point in range(entry[0].shape[0])
            ]
        finally:
            self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.locations)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole:
            if orientation == QtCore.Qt.Horizontal:
                return self.headers[section]
            return str(section + 1)
        return None

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        run_index, point = self.locations[index.row()]
        entry = self.runs[run_index]
        col = index.column()
        if role == QtCore.Qt.TextAlignmentRole:
            return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if role != QtCore.Qt.DisplayRole:
            return None
        if col == 0:
            return str(point + 1)
        if self.multi and col == 1:
            return entry[5]
        if col == len(self.headers) - 1:
            names = [name.strip() for name in entry[1].split(",")][1:]
            return " ".join(
                f"{name}:{entry[2][point, k]}"
                for k, name in enumerate(names)
                if entry[2][point, k] != "N"
            ) or "N"
        data_col = col - 1 - int(self.multi)
        return f"{entry[0][point, data_col] / self.factors[data_col]:.7g}"


class SweepMainWindow(QtWidgets.QMainWindow):
    """Main application window for configuring, running, plotting, and inspecting sweeps."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("HP 4145 / 4145B — Sweep Controller")
        self.resize(1350, 850)
        # Re-entrancy guard used when synchronizing SMU roles and stepped-offset widgets.
        self.updating = False
        self.thread = None
        self.worker = None
        self.cancel_event = None
        self.batch = []
        self.history = []
        self.channel_checks = {}
        self.result_message = ""
        self.result_error = False
        self.build_ui()
        self.connect_signals()
        self.validate()

    def combo(self, choices):
        """Create a QComboBox populated from (data, label) pairs."""
        widget = QtWidgets.QComboBox()
        for value, label in choices:
            widget.addItem(label, value)
        return widget

    def group(self, title, parent_layout):
        """Create a titled QGroupBox with an inner QGridLayout."""
        box = QtWidgets.QGroupBox(title)
        grid = QtWidgets.QGridLayout(box)
        parent_layout.addWidget(box)
        return box, grid

    def build_ui(self):
        """Construct the split-pane UI (left configuration form, right plot/table/log tabs)."""
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("HP 4145 / 4145B Semiconductor Analyzer")
        title.setStyleSheet("font-size: 17px; font-weight: bold;")
        top.addWidget(title)
        top.addStretch()
        self.presets = QtWidgets.QComboBox()
        self.presets.addItems(PRESETS)
        top.addWidget(self.presets)
        root.addLayout(top)

        splitter = QtWidgets.QSplitter()
        root.addWidget(splitter, 1)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.configuration = QtWidgets.QWidget()
        controls = QtWidgets.QVBoxLayout(self.configuration)
        scroll.setWidget(self.configuration)
        left_layout.addWidget(scroll)
        splitter.addWidget(left)

        _, grid = self.group("1. Connection", controls)
        self.address = QtWidgets.QLineEdit("GPIB0::16::INSTR")
        self.mock = QtWidgets.QCheckBox("Offline simulator")
        self.refresh = QtWidgets.QPushButton("List VISA resources")
        grid.addWidget(QtWidgets.QLabel("VISA address"), 0, 0)
        grid.addWidget(self.address, 0, 1)
        grid.addWidget(self.refresh, 0, 2)
        grid.addWidget(self.mock, 1, 0, 1, 3)

        _, grid = self.group("2. SMU roles", controls)
        for col, text in enumerate(["SMU", "Role", "Value", "Compliance"]):
            grid.addWidget(QtWidgets.QLabel(text), 0, col)
        self.smus = {}
        defaults = ["sweep-v", "com", "off", "off"]
        for smu in ts.SMUS:
            role = self.combo(ROLES)
            role.setCurrentIndex(role.findData(defaults[smu - 1]))
            value = QtWidgets.QLineEdit("0")
            comp = QtWidgets.QLineEdit("1m")
            value.setMaximumWidth(95)
            comp.setMaximumWidth(95)
            self.smus[smu] = (role, value, comp)
            grid.addWidget(QtWidgets.QLabel(f"SMU{smu}"), smu, 0)
            grid.addWidget(role, smu, 1)
            grid.addWidget(value, smu, 2)
            grid.addWidget(comp, smu, 3)

        _, grid = self.group("3. Primary sweep", controls)
        self.sweep_type = self.combo([("linear", "Linear"), ("log", "Log")])
        self.start = QtWidgets.QLineEdit("-2")
        self.stop = QtWidgets.QLineEdit("2")
        self.step = QtWidgets.QLineEdit("0.1")
        self.compliance = QtWidgets.QLineEdit("1m")
        for row, (label, widget) in enumerate([
            ("Type", self.sweep_type), ("Start", self.start),
            ("Stop", self.stop), ("Step", self.step),
            ("Sweep compliance", self.compliance),
        ]):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)

        _, grid = self.group("4. Software-stepped curve family", controls)
        self.multi = QtWidgets.QCheckBox("Enable stepped offsets")
        self.offset_smu = self.combo([
            (smu, f"SMU{smu}") for smu in ts.SMUS
        ])
        self.offset_smu.setCurrentIndex(2)
        self.offset_mode = self.combo([
            ("voltage", "Voltage (V)"), ("current", "Current (A)")
        ])
        self.offset_spec = self.combo([
            ("range", "Start / stop / step"), ("list", "Custom list")
        ])
        self.offset_start = QtWidgets.QLineEdit("0.5")
        self.offset_stop = QtWidgets.QLineEdit("2.5")
        self.offset_step = QtWidgets.QLineEdit("0.5")
        self.offset_list = QtWidgets.QLineEdit("0.5, 1, 1.5, 2, 2.5")
        self.offset_comp = QtWidgets.QLineEdit("1u")
        grid.addWidget(self.multi, 0, 0, 1, 2)
        self.offset_widgets = [
            self.offset_smu, self.offset_mode, self.offset_spec,
            self.offset_start, self.offset_stop, self.offset_step,
            self.offset_list, self.offset_comp,
        ]
        for row, (label, widget) in enumerate(zip(
            ["Probe", "Mode", "Definition", "Start", "Stop", "Step",
             "Custom offsets", "Offset compliance"],
            self.offset_widgets,
        ), start=1):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)

        _, grid = self.group("5. Timing", controls)
        self.integration = self.combo([
            ("S", "Short"), ("M", "Medium"), ("L", "Long")
        ])
        self.integration.setCurrentIndex(1)
        self.delay = QtWidgets.QLineEdit("0.01")
        self.hold = QtWidgets.QLineEdit("0")
        for row, (label, widget) in enumerate([
            ("Integration", self.integration),
            ("Delay (s)", self.delay), ("Hold (s)", self.hold),
        ]):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)

        _, grid = self.group("6. Output", controls)
        self.save_enabled = QtWidgets.QCheckBox("Save CSV")
        self.save_enabled.setChecked(True)
        self.save_path = QtWidgets.QLineEdit("CSV/hardware_sweep.csv")
        self.save_browse = QtWidgets.QPushButton("Browse")
        grid.addWidget(self.save_enabled, 0, 0)
        grid.addWidget(self.save_path, 0, 1)
        grid.addWidget(self.save_browse, 0, 2)
        controls.addStretch()

        self.validation = QtWidgets.QLabel()
        self.validation.setWordWrap(True)
        left_layout.addWidget(self.validation)
        actions = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run sweep")
        self.stop_button = QtWidgets.QPushButton("Stop after current curve")
        self.stop_button.setEnabled(False)
        actions.addWidget(self.run_button)
        actions.addWidget(self.stop_button)
        left_layout.addLayout(actions)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        splitter.addWidget(right)
        splitter.setSizes([510, 840])
        self.banner = QtWidgets.QLabel("Ready")
        self.banner.setWordWrap(True)
        right_layout.addWidget(self.banner)
        self.tabs = QtWidgets.QTabWidget()
        right_layout.addWidget(self.tabs)

        plot_page = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_page)
        plot_controls = QtWidgets.QHBoxLayout()
        self.y_scale = self.combo([
            ("linear", "Linear Y"), ("log", "Log |Y|")
        ])
        self.overlay = QtWidgets.QCheckBox("Keep previous compatible curves")
        self.clear_history = QtWidgets.QPushButton("Keep current batch only")
        plot_controls.addWidget(self.y_scale)
        plot_controls.addWidget(self.overlay)
        plot_controls.addWidget(self.clear_history)
        plot_layout.addLayout(plot_controls)
        self.channel_layout = QtWidgets.QHBoxLayout()
        plot_layout.addLayout(self.channel_layout)
        self.figure = Figure(figsize=(7, 5), tight_layout=True)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.figure)
        plot_layout.addWidget(self.canvas, 1)
        plot_layout.addWidget(NavigationToolbar2QT(self.canvas, self))
        self.tabs.addTab(plot_page, "Plot")

        table_page = QtWidgets.QWidget()
        table_layout = QtWidgets.QVBoxLayout(table_page)
        self.filter = QtWidgets.QComboBox()
        self.filter.addItem("All completed curves", None)
        table_layout.addWidget(self.filter)
        self.model = MeasurementModel(self)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setDefaultSectionSize(135)
        self.table.horizontalHeader().setStretchLastSection(True)
        table_layout.addWidget(self.table)
        self.tabs.addTab(table_page, "Data")

        log_page = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_page)
        clear_log = QtWidgets.QPushButton("Clear log")
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_BLOCKS)
        self.log_view.setFont(QtGui.QFont("Monospace", 10))
        clear_log.clicked.connect(self.log_view.clear)
        log_layout.addWidget(clear_log)
        log_layout.addWidget(self.log_view)
        self.tabs.addTab(log_page, "Log")

        command_row = QtWidgets.QHBoxLayout()
        self.command = QtWidgets.QLineEdit()
        self.command.setReadOnly(True)
        copy_button = QtWidgets.QPushButton("Copy first-curve CLI")
        copy_button.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(
                self.command.text()
            )
        )
        command_row.addWidget(self.command)
        command_row.addWidget(copy_button)
        root.addLayout(command_row)
        self.draw_plot()

    def connect_signals(self):
        self.presets.currentTextChanged.connect(self.apply_preset)
        for smu, (role, value, comp) in self.smus.items():
            role.currentIndexChanged.connect(
                lambda _, n=smu: self.role_changed(n)
            )
            value.textChanged.connect(self.validate)
            comp.textChanged.connect(
                lambda _, n=smu: self.comp_changed(n)
            )
        self.multi.toggled.connect(self.multi_changed)
        self.offset_smu.currentIndexChanged.connect(self.offset_changed)
        self.offset_mode.currentIndexChanged.connect(self.offset_changed)
        self.offset_comp.textChanged.connect(self.offset_comp_changed)

        for widget in [
            self.address, self.start, self.stop, self.step,
            self.compliance, self.delay, self.hold,
            self.offset_start, self.offset_stop, self.offset_step,
            self.offset_list, self.save_path,
        ]:
            widget.textChanged.connect(self.validate)
        for widget in [
            self.sweep_type, self.integration, self.offset_spec,
        ]:
            widget.currentIndexChanged.connect(self.validate)
        for widget in [self.mock, self.save_enabled]:
            widget.toggled.connect(self.validate)

        self.run_button.clicked.connect(self.start_sweep)
        self.stop_button.clicked.connect(self.stop_sweep)
        self.refresh.clicked.connect(self.scan_visa)
        self.y_scale.currentIndexChanged.connect(self.draw_plot)
        self.clear_history.clicked.connect(self.keep_batch)
        self.filter.currentIndexChanged.connect(self.update_table)
        self.save_browse.clicked.connect(
            lambda: self.browse(self.save_path, self.save_enabled)
        )

    def set_role(self, smu, key):
        widget = self.smus[smu][0]
        widget.setCurrentIndex(widget.findData(key))

    def role_changed(self, smu):
        """Keep SMU step-v/step-i roles synchronized with the stepped-offset panel."""
        if self.updating:
            return
        self.updating = True
        try:
            role, _, comp = self.smus[smu]
            key = role.currentData()
            if key in ("step-v", "step-i"):
                for other, controls in self.smus.items():
                    old = controls[0].currentData()
                    if other != smu and old in ("step-v", "step-i"):
                        self.set_role(
                            other, "const-v" if old == "step-v" else "const-i"
                        )
                self.multi.setChecked(True)
                self.offset_smu.setCurrentIndex(
                    self.offset_smu.findData(smu)
                )
                self.offset_mode.setCurrentIndex(
                    self.offset_mode.findData(
                        "voltage" if key == "step-v" else "current"
                    )
                )
                self.offset_comp.setText(comp.text())
            elif not any(
                controls[0].currentData() in ("step-v", "step-i")
                for controls in self.smus.values()
            ):
                self.multi.setChecked(False)
        finally:
            self.updating = False
        self.validate()

    def multi_changed(self, checked):
        if self.updating:
            return
        self.updating = True
        try:
            for smu, controls in self.smus.items():
                role = controls[0].currentData()
                if role in ("step-v", "step-i"):
                    self.set_role(
                        smu, "const-v" if role == "step-v" else "const-i"
                    )
            if checked:
                smu = self.offset_smu.currentData()
                self.set_role(
                    smu,
                    "step-v" if self.offset_mode.currentData() == "voltage"
                    else "step-i",
                )
                self.smus[smu][2].setText(self.offset_comp.text())
        finally:
            self.updating = False
        self.validate()

    def offset_changed(self, *_):
        if self.updating:
            return
        if self.multi.isChecked():
            self.multi_changed(True)
        else:
            self.validate()

    def comp_changed(self, smu):
        if self.updating:
            return
        if self.multi.isChecked() and self.offset_smu.currentData() == smu:
            self.updating = True
            try:
                self.offset_comp.setText(self.smus[smu][2].text())
            finally:
                self.updating = False
        self.validate()

    def offset_comp_changed(self, *_):
        if self.updating:
            return
        if self.multi.isChecked():
            self.updating = True
            try:
                self.smus[self.offset_smu.currentData()][2].setText(
                    self.offset_comp.text()
                )
            finally:
                self.updating = False
        self.validate()

    def apply_preset(self, name):
        """Populate all form fields from a named preset in PRESETS."""
        settings = PRESETS.get(name)
        if not settings:
            return
        self.updating = True
        try:
            for smu, (key, value, comp) in enumerate(
                settings["roles"], start=1
            ):
                self.set_role(smu, key)
                self.smus[smu][1].setText(value or "0")
                self.smus[smu][2].setText(
                    comp or ("2" if key == "const-i" else "1m")
                )
            for key, widget in [
                ("start", self.start), ("stop", self.stop),
                ("step", self.step), ("comp", self.compliance),
                ("delay", self.delay), ("hold", self.hold),
            ]:
                widget.setText(settings[key])
            self.sweep_type.setCurrentIndex(
                self.sweep_type.findData(settings["sweep_type"])
            )
            self.integration.setCurrentIndex(
                self.integration.findData(settings["integration"])
            )
            # Never silently switch an offline session onto hardware.
            if settings.get("mock"):
                self.mock.setChecked(True)
            self.multi.setChecked(settings["multi"])
            if settings["multi"]:
                self.offset_smu.setCurrentIndex(
                    self.offset_smu.findData(settings["offset_smu"])
                )
                self.offset_mode.setCurrentIndex(
                    self.offset_mode.findData(settings["offset_mode"])
                )
                self.offset_spec.setCurrentIndex(
                    self.offset_spec.findData(
                        settings.get("offset_spec", "range")
                    )
                )
                for key, widget in [
                    ("offset_start", self.offset_start),
                    ("offset_stop", self.offset_stop),
                    ("offset_step", self.offset_step),
                    ("offset_list", self.offset_list),
                    ("offset_comp", self.offset_comp),
                ]:
                    widget.setText(settings[key])
        finally:
            self.updating = False
        self.validate()

    def offsets(self):
        """Parse stepped-offset parameters for multi-curve family sweeps, or return None."""
        if not self.multi.isChecked():
            return None
        if self.offset_spec.currentData() == "list":
            tokens = self.offset_list.text().split(",")
            if not 1 <= len(tokens) <= MAX_OFFSETS:
                raise ValueError("Enter 1–50 offsets")
            values = [
                number(token, f"Offset {index + 1}")
                for index, token in enumerate(tokens)
            ]
        else:
            start = number(self.offset_start.text(), "Offset start")
            stop = number(self.offset_stop.text(), "Offset stop")
            step = number(self.offset_step.text(), "Offset step")
            if step == 0:
                raise ValueError("Offset step cannot be zero")
            signed = abs(step) if stop >= start else -abs(step)
            ratio = (stop - start) / signed
            if not math.isfinite(ratio) or not 0 <= ratio <= 49 + 1e-6:
                raise ValueError("Offset range must contain 1–50 values")
            if abs(ratio - round(ratio)) > 1e-6:
                raise ValueError("Offset range must be a whole number of steps")
            values = [
                start + index * signed for index in range(int(round(ratio)) + 1)
            ]
            values[-1] = stop
        comp = number(self.offset_comp.text(), "Offset compliance")
        if comp <= 0:
            raise ValueError("Offset compliance must be positive")
        return dict(
            smu=self.offset_smu.currentData(),
            mode=self.offset_mode.currentData(),
            values=values, comp=comp,
        )

    def build_jobs(self):
        """Validate UI inputs via parameter_analyzer_sweep.build_setup() and build sweep jobs."""
        info = self.offsets()
        delay = number(self.delay.text(), "Delay")
        hold = number(self.hold.text(), "Hold")
        if not 0 <= delay <= 6.5 or not 0 <= hold <= 650:
            raise ValueError("Delay: 0–6.5 s; hold: 0–650 s")
        start = number(self.start.text(), "Sweep start")
        stop = number(self.stop.text(), "Sweep stop")
        sweep_type = self.sweep_type.currentData()
        step = number(self.step.text(), "Sweep step") if sweep_type == "linear" else 0
        comp = number(self.compliance.text(), "Sweep compliance")

        save = self.save_path.text().strip()
        if self.save_enabled.isChecked() and not save:
            raise ValueError("Enter a CSV path or disable CSV saving")
        save = output_path(save) if self.save_enabled.isChecked() else ""

        jobs = []
        for offset in info["values"] if info else [None]:
            tokens = {}
            for smu, (role_widget, value_widget, comp_widget) in self.smus.items():
                role = role_widget.currentData()
                if role in ("off", "com", "sweep-v", "sweep-i"):
                    token = role
                else:
                    if role in ("step-v", "step-i"):
                        if info is None or smu != info["smu"]:
                            raise ValueError("Stepped SMU and offset probe mismatch")
                        value = offset
                    else:
                        value = number(value_widget.text(), f"SMU{smu} value")
                    limit = number(comp_widget.text(), f"SMU{smu} compliance")
                    prefix = "v" if role in ("const-v", "step-v") else "i"
                    token = f"{prefix}:{value:.17g}:{limit:.17g}"
                tokens[smu] = token

            args = argparse.Namespace(
                address=self.address.text().strip() or "GPIB0::16::INSTR",
                smu1=tokens[1], smu2=tokens[2],
                smu3=tokens[3], smu4=tokens[4],
                start=start, stop=stop, step=step,
                sweep_type=sweep_type, compliance=comp,
                integration=self.integration.currentData(),
                delay=delay, hold=hold,
                save_csv=save,
                check_visa=False, mock=self.mock.isChecked(),
            )
            setup = ts.build_setup(args)
            if setup["n_points"] > MAX_POINTS:
                raise ValueError(
                    "GUI limit: 512 points per curve. "
                    "Reduce the range or increase the step."
                )
            if info:
                expected = "step-v" if info["mode"] == "voltage" else "step-i"
                if self.smus[info["smu"]][0].currentData() != expected:
                    raise ValueError("Offset mode and SMU role mismatch")
                unit = "V" if info["mode"] == "voltage" else "A"
                label = f"SMU{info['smu']}={offset:g} {unit}"
            else:
                label = "Single sweep"
            jobs.append((args, setup, label, offset))
        return jobs, info

    def cli(self, args):
        """Format an equivalent parameter_analyzer_sweep.py CLI command for the first curve."""
        import shlex
        parts = [sys.executable, os.path.join(PROJECT_ROOT, "parameter_analyzer_sweep.py")]
        for key in [
            "address", "smu1", "smu2", "smu3", "smu4", "start",
            "stop", "sweep_type", "compliance", "integration",
            "delay", "hold",
        ]:
            parts.extend(["--" + key.replace("_", "-"), str(getattr(args, key))])
        if args.sweep_type == "linear":
            parts.extend(["--step", str(args.step)])
        if args.save_csv:
            parts.extend(["--save-csv", str(args.save_csv)])
        if args.mock:
            parts.append("--mock")
        return " ".join(shlex.quote(part) for part in parts)

    def validate(self, *_):
        """Enable/disable dependent inputs and update live validation and CLI preview."""
        if self.updating:
            return
        multi = self.multi.isChecked()
        for widget in self.offset_widgets:
            widget.setEnabled(multi)
        is_range = self.offset_spec.currentData() == "range"
        for widget in [self.offset_start, self.offset_stop, self.offset_step]:
            widget.setEnabled(multi and is_range)
        self.offset_list.setEnabled(multi and not is_range)
        self.step.setEnabled(self.sweep_type.currentData() == "linear")
        self.save_path.setEnabled(self.save_enabled.isChecked())
        for role, value, comp in self.smus.values():
            key = role.currentData()
            value.setEnabled(key in ("const-v", "const-i"))
            comp.setEnabled(key in ("const-v", "const-i", "step-v", "step-i"))
        try:
            jobs, _ = self.build_jobs()
            self.command.setText(self.cli(jobs[0][0]))
            points = jobs[0][1]["n_points"]
            self.validation.setText(
                f"Valid: {len(jobs)} curve(s), {points} points each."
            )
            self.validation.setStyleSheet("color: #166534;")
            self.run_button.setEnabled(self.thread is None)
        except Exception as exc:
            self.validation.setText(str(exc))
            self.validation.setStyleSheet("color: #b91c1c;")
            self.command.clear()
            self.run_button.setEnabled(False)

    def browse(self, widget, enabled):
        os.makedirs(CSV_DIR, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Select output CSV",
            output_path(widget.text() or "CSV/hardware_sweep.csv"),
            "CSV files (*.csv)",
        )
        if path:
            widget.setText(path)
            enabled.setChecked(True)

    def scan_visa(self):
        if self.thread is not None:
            return
        manager = None
        try:
            import pyvisa
            manager = pyvisa.ResourceManager()
            resources = manager.list_resources()
            self.append_log("VISA resources: " + (", ".join(resources) or "none"))
            self.tabs.setCurrentIndex(2)
        except Exception:
            self.append_log(traceback.format_exc())
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    self.append_log(traceback.format_exc())

    def append_log(self, text):
        self.log_view.appendPlainText(str(text))

    def start_sweep(self):
        """Confirm file overwrites, reset batch state, and launch SweepWorker in a QThread."""
        if self.thread is not None:
            return
        try:
            jobs, info = self.build_jobs()
            args = jobs[0][0]
            paths = []
            if args.save_csv:
                stem = os.path.splitext(args.save_csv)[0]
                paths.extend([
                    args.save_csv, stem + "_flags.csv",
                    stem + "_setup.txt",
                ])
                if info:
                    paths.append(stem + "_family.csv")
            existing = [path for path in paths if os.path.exists(path)]
            if existing:
                answer = QtWidgets.QMessageBox.question(
                    self, "Overwrite output files?",
                    "These files may be replaced:\n" + "\n".join(existing),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if answer != QtWidgets.QMessageBox.Yes:
                    return
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Invalid setup", str(exc))
            return

        self.batch = []
        self.model.set_runs([])
        self.rebuild_filter()
        if not self.overlay.isChecked():
            self.history = []
        self.draw_plot()
        self.result_message = ""
        self.result_error = False
        self.cancel_event = threading.Event()
        thread = QtCore.QThread(self)
        worker = SweepWorker(jobs, info, self.cancel_event)
        self.thread = thread
        self.worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self.append_log)
        worker.progress.connect(self.banner.setText)
        worker.curve.connect(self.receive_curve)
        worker.completed.connect(self.receive_result)
        # quit() is thread-safe; direct connection also works when GUI
        # processing is temporarily busy.
        worker.completed.connect(thread.quit, QtCore.Qt.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.thread_finished)
        self.configuration.setEnabled(False)
        self.presets.setEnabled(False)
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.banner.setText("Starting…")
        thread.start()

    def stop_sweep(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.stop_button.setEnabled(False)
            self.banner.setText(
                "Stop requested: waiting for the current curve and saving. "
                "This does not switch off instrument outputs."
            )

    def receive_curve(self, entry):
        try:
            self.batch.append(entry)
            self.history.append(entry)
            self.history = self.history[-MAX_HISTORY:]
            if len(self.batch) == 1:
                self.rebuild_channels(entry[1])
            self.rebuild_filter()
            self.update_table()
            self.draw_plot()
        except Exception:
            self.append_log(traceback.format_exc())
            self.stop_sweep()

    def receive_result(self, message, error):
        self.result_message = message
        self.result_error = error
        self.banner.setText(message)
        self.append_log(message)

    def thread_finished(self):
        thread = self.thread
        self.thread = None
        self.worker = None
        self.cancel_event = None
        if thread is not None:
            thread.deleteLater()
        self.configuration.setEnabled(True)
        self.presets.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.validate()
        if self.result_error:
            self.tabs.setCurrentIndex(2)

    def closeEvent(self, event):
        if self.thread is not None:
            self.stop_sweep()
            self.banner.setText(
                "Close postponed until acquisition and saving finish. "
                "Close again afterward; verify outputs on the analyzer."
            )
            event.ignore()
            return
        event.accept()

    def keep_batch(self):
        self.history = list(self.batch[-MAX_HISTORY:])
        self.draw_plot()

    def rebuild_channels(self, header):
        previous = {
            name: widget.isChecked()
            for name, widget in self.channel_checks.items()
        }
        while self.channel_layout.count():
            item = self.channel_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.channel_checks = {}
        for index, name in enumerate(
            [name.strip() for name in header.split(",")][1:]
        ):
            widget = QtWidgets.QCheckBox(name)
            widget.setChecked(previous.get(name, index == 0))
            widget.toggled.connect(self.draw_plot)
            self.channel_checks[name] = widget
            self.channel_layout.addWidget(widget)
        self.channel_layout.addStretch()

    def rebuild_filter(self):
        previous = self.filter.currentData()
        self.filter.blockSignals(True)
        try:
            self.filter.clear()
            self.filter.addItem("All completed curves", None)
            for index, entry in enumerate(self.batch):
                self.filter.addItem(
                    f"Curve {index + 1}: {entry[5]}", index
                )
            if previous is not None:
                found = self.filter.findData(previous)
                if found >= 0:
                    self.filter.setCurrentIndex(found)
        finally:
            self.filter.blockSignals(False)

    def update_table(self, *_):
        selected = self.filter.currentData()
        runs = (
            [self.batch[selected]]
            if isinstance(selected, int) and 0 <= selected < len(self.batch)
            else self.batch
        )
        try:
            self.model.set_runs(runs)
        except Exception:
            self.append_log(traceback.format_exc())

    def draw_plot(self, *_):
        """Render compatible curves in SI-scaled units and highlight flagged points."""
        try:
            self.ax.clear()
            if not self.history:
                self.ax.set_title("Run a sweep to display measurement curves")
                self.ax.set_xlabel("Sweep variable")
                self.ax.set_ylabel("Measured output")
                self.ax.grid(True, alpha=0.3)
                self.canvas.draw_idle()
                return

            latest = self.history[-1]
            sweep_name = latest[1].split(",")[0].strip()
            selected = {
                name for name, widget in self.channel_checks.items()
                if widget.isChecked()
            }
            if not selected:
                self.ax.set_title("Select a channel")
                self.canvas.draw_idle()
                return

            runs = [
                entry for entry in self.history
                if entry[1].split(",")[0].strip() == sweep_name
                and entry[4].sweep_type == latest[4].sweep_type
            ]
            curves = []
            for entry in runs:
                names = [name.strip() for name in entry[1].split(",")][1:]
                for col, name in enumerate(names):
                    if name in selected:
                        curves.append((entry, col, name))
            if not curves:
                self.canvas.draw_idle()
                return

            units = sorted({ts.unit_of(name) for _, _, name in curves})
            if len(units) > 1:
                self.ax.set_title(
                    "Select only current channels or only voltage channels"
                )
                self.canvas.draw_idle()
                return

            all_x = np.concatenate([entry[0][:, 0] for entry, _, _ in curves])
            all_y = np.concatenate([
                entry[0][:, col + 1] for entry, col, _ in curves
            ])
            fx, xlabel = scale(all_x, ts.unit_of(sweep_name))
            log_y = self.y_scale.currentData() == "log"
            fy, ylabel = (1.0, units[0]) if log_y else scale(all_y, units[0])
            show_legend = len(curves) <= 15

            for entry, col, name in curves:
                data, _, status, _, _, label, _ = entry
                x = data[:, 0] / fx
                y = data[:, col + 1].copy()
                if log_y:
                    y = np.abs(y)
                    y[y == 0] = np.nan
                else:
                    y /= fy
                self.ax.plot(
                    x, y, marker="o", markersize=2.5, linewidth=1.4,
                    label=f"{name} ({label})" if show_legend else None,
                )
                bad = (
                    (status[:, col] != "N")
                    & np.isfinite(x) & np.isfinite(y)
                )
                if bad.any():
                    self.ax.scatter(
                        x[bad], y[bad], s=25, color="#f59e0b",
                        edgecolors="black", zorder=5,
                    )

            finite_x = all_x[np.isfinite(all_x)]
            if latest[4].sweep_type == "log" and finite_x.size:
                if np.all(finite_x > 0):
                    self.ax.set_xscale("log")
                elif np.all(finite_x < 0):
                    self.ax.set_xscale(
                        "symlog",
                        linthresh=float(np.min(np.abs(finite_x)) / fx / 10),
                    )
            if log_y and np.any(np.isfinite(all_y) & (all_y != 0)):
                self.ax.set_yscale("log")
            self.ax.set_xlabel(f"{sweep_name} set ({xlabel})")
            self.ax.set_ylabel(
                f"|Measured| ({ylabel})" if log_y else f"Measured ({ylabel})"
            )
            self.ax.set_title(f"HP 4145B — {len(runs)} compatible curve(s)")
            self.ax.grid(True, which="both", alpha=0.3)
            if show_legend:
                self.ax.legend(fontsize=8)
            self.canvas.draw_idle()
        except Exception:
            self.append_log(traceback.format_exc())


def main():
    """Initialize the Qt application, install a GUI exception hook, and run the event loop."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(
            [argument for argument in sys.argv if argument != "--gui"]
        )
    app.setStyle("Fusion")
    window = SweepMainWindow()

    def exception_handler(exc_type, value, tb):
        message = "".join(traceback.format_exception(exc_type, value, tb))
        sys.stderr.write(message)
        try:
            window.append_log("[Unhandled GUI error]\n" + message)
            if window.thread is not None:
                window.stop_sweep()
        except Exception:
            pass

    sys.excepthook = exception_handler
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
