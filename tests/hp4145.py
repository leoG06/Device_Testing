try:
    from . import equipment  # when imported as dopes.equipment_control.hp4145
except ImportError:
    import equipment  # when the equipment_control folder itself is on sys.path
import pyvisa
import time
import numpy as np


class hp4145(equipment.equipment):

    """Class to control HP4145 / 4145B semiconductor parameter analyzer (System Mode).

    Patched version. Changes vs. the original DOpeS driver (same public API):
      - waits for the Data Ready bit of the status byte before reading data
        (manual Sample Program 1, Fig. 3-56) instead of reading immediately
      - removed "EC 1" (not a 4145B program code, Fig. 3-52 -> syntax error)
      - removed the VAR1 command that was sent before "SS" (wrong page)
      - turns off VS1/VS2/VM1/VM2, which the power-on GENL setup leaves enabled
      - sweep point count computed by rounding (np.arange could add an extra point)
      - segmentation no longer crashes when the point count is a multiple of the
        segment size, and supports start > stop (signed step, as on the front panel)
      - COMMON SMUs are not given a data column (fixes duplicate column / crash
        when a COMMON SMU is numbered before the measured SMUs)
      - constant bias written with %.6E (was %.2f, which rounded 1e-6 A to 0)
      - keeps the per-point status letters (N, C, T, X, V, L - Fig. 3-53) in
        self.last_status and warns when any point is not "N"
    """

    model = "HP4145"
    company = "Keysight"
    url = "https://www.keysight.com/dk/en/assets/9018-07935/service-manuals/9018-07935.pdf"

    STATUS_MEANING = {
        "N": "normal",
        "L": "interval too short",
        "V": "A/D converter saturation",
        "X": "oscillation",
        "C": "this channel reached compliance",
        "T": "another channel reached compliance",
    }

    def initialize(self, smu_type={"SMU1": "voltage", "SMU2": "voltage", "SMU3": "common", "SMU4": "common"},
                   smu_used={"SMU1": "on", "SMU2": "on", "SMU3": "on", "SMU4": "on"},
                   smu_master=1, smu_bias={"SMU1": 0, "SMU2": 0, "SMU3": 0, "SMU4": 0},
                   smu_compliance={"SMU1": 1e-6, "SMU2": 1e-6, "SMU3": 1e-6, "SMU4": 1e-6},
                   sweep_param={"start": 0, "stop": 0, "step": 0}, sweep_type="linear",
                   integration_mode="S", delay_time=0, hold_time=0):
        """ Function to initialize the HP4145 equipment with the desired settings

            args:
                - smu_type (dictionary) : for each SMU ("SMU1".."SMU4") the bias type ("voltage", "current" or "common")
                - smu_used (dictionary) : for each SMU ("SMU1".."SMU4") "on" or "off"
                - smu_master (int) : SMU that performs the VAR1 sweep
                - smu_bias (dictionary) : constant bias for each non-master SMU (V or A)
                - smu_compliance (dictionary) : compliance for each SMU (A for voltage sources, V for current sources)
                - sweep_param (dictionary) : "start", "stop" and "step" of the sweep
                - sweep_type (string) : "linear" or "log" (log = 10 points/decade)
                - integration_mode (string) : "S" short, "M" medium, "L" long
                - delay_time (scalar) : wait between setting each step and measuring (0 - 6.5 s)
                - hold_time (scalar) : wait before the sweep starts (0 - 650 s)
        """

        self.smu_type = smu_type
        self.sweep_type = sweep_type
        self.smu_used = smu_used
        self.smu_master = smu_master
        self.sweep_param = sweep_param
        self.smu_compliance = smu_compliance
        self.smu_bias = smu_bias
        self.integration_mode = integration_mode
        self.delay_time = delay_time
        self.hold_time = hold_time
        self.last_status = None

        if smu_type["SMU%d" % smu_master] == "common":
            raise ValueError("The master (VAR1) SMU cannot be of type 'common'.")
        if sweep_type == "log" and (sweep_param["start"] == 0 or sweep_param["stop"] == 0
                                    or np.sign(sweep_param["start"]) != np.sign(sweep_param["stop"])):
            raise ValueError("Log sweep needs non-zero START and STOP of the same sign (manual Fig. 3-24).")

        index_measurement_type = {"sweep": 1, "constant": 3}
        index_smu_type = {"voltage": 1, "current": 2, "common": 3}
        integration_dictionnary = {"S": "IT1", "M": "IT2", "L": "IT3"}
        res = self.pyvisa_resource
        try:
            res.write("BC")   # clear HP-IB data buffer and Data Ready bit
            res.write("DR1")  # Data Ready sets bit 1 of the status byte and RQS

            res.write("DE")   # CHANNEL DEFINITION page
            res.write("CH%d, 'V%d', 'I%d', %d, %d" % (smu_master, smu_master, smu_master,
                      index_smu_type[smu_type["SMU%d" % smu_master]], index_measurement_type["sweep"]))
            for n in range(1, 5):
                if n == smu_master:
                    continue
                if smu_used["SMU%d" % n] == "off":
                    res.write("CH%d" % n)  # channel off (NOT USE)
                else:
                    res.write("CH%d, 'V%d', 'I%d', %d, %d" % (n, n, n, index_smu_type[smu_type["SMU%d" % n]],
                                                             index_measurement_type["constant"]))
            res.write("VS1; VS2; VM1; VM2")  # Vs and Vm not used (GENL leaves them on)

            res.write("SS")   # SOURCE SETUP page
            self._write_var1(sweep_param["start"], sweep_param["stop"], self._signed_step())
            for n in range(1, 5):
                if n != smu_master and smu_used["SMU%d" % n] == "on":
                    if smu_type["SMU%d" % n] == "voltage":
                        res.write("VC%d, %.6E, %.6E" % (n, smu_bias["SMU%d" % n], smu_compliance["SMU%d" % n]))
                    elif smu_type["SMU%d" % n] == "current":
                        res.write("IC%d, %.6E, %.6E" % (n, smu_bias["SMU%d" % n], smu_compliance["SMU%d" % n]))
            res.write("HT %.2f" % hold_time)
            res.write("DT %.3f" % delay_time)
            res.write(integration_dictionnary[integration_mode])

            res.write("SM")   # MEAS & DISP MODE SETUP page
            res.write("DM2")  # LIST display
            res.write("LI %s" % ",".join("'%s'" % name for name in self._measured_names()))
        except pyvisa.VisaIOError:
            print("/!\\ VisaIOError : timeout expired")
            self.pyvisa_resource.close()
            raise

    # ------------------------------------------------------------------ helpers
    def _measured_names(self):
        """Names of the monitored quantities, in SMU order (I for V-sources, V for I-sources)."""
        names = []
        for n in range(1, 5):
            if self.smu_used["SMU%d" % n] == "on":
                if self.smu_type["SMU%d" % n] == "voltage":
                    names.append("I%d" % n)
                elif self.smu_type["SMU%d" % n] == "current":
                    names.append("V%d" % n)
        return names

    def _signed_step(self):
        step = abs(self.sweep_param["step"])
        return -step if self.sweep_param["stop"] < self.sweep_param["start"] else step

    def _write_var1(self, start, stop, step):
        prefix = "VR" if self.smu_type["SMU%d" % self.smu_master] == "voltage" else "IR"
        comp = self.smu_compliance["SMU%d" % self.smu_master]
        if self.sweep_type == "linear":
            self.pyvisa_resource.write("%s1, %.6E, %.6E, %.6E, %.6E" % (prefix, start, stop, step, comp))
        else:  # LOG 10 points/decade, STEP omitted
            self.pyvisa_resource.write("%s2, %.6E, %.6E, %.6E" % (prefix, start, stop, comp))

    def _sweep_list(self):
        start, stop = self.sweep_param["start"], self.sweep_param["stop"]
        if self.sweep_type == "log":
            n = int(np.floor(round(10 * abs(np.log10(stop / start)), 6))) + 1
            return start * 10 ** (np.sign(np.log10(stop / start)) * np.arange(n) / 10)
        step = self._signed_step()
        n = int(round((stop - start) / step)) + 1 if step != 0 else 1
        return np.round(start + step * np.arange(n), 9)

    def _wait_data_ready(self, n_points):
        """Serial-poll until bit 1 (Data Ready, value 1) of the status byte is set."""
        per_point = {"S": 0.03, "M": 0.08, "L": 0.45}[self.integration_mode] + self.delay_time
        limit = 30 + self.hold_time + 3 * n_points * per_point
        t0 = time.time()
        # Status byte (Fig. 3-55): 0x01 Data Ready, 0x02 Syntax Error, 0x04 END,
        # 0x08 Illegal Program, 0x10 Busy, 0x20 Self-Test Fail, 0x40 RQS, 0x80 Emergency.
        # A serial poll resets every bit except Busy, so each poll is acted on at once.
        while True:
            stb = self.pyvisa_resource.read_stb()
            if stb & 0x80:
                if stb & 0x02:
                    reason = "fixture lid open (or shorting connector missing)"
                elif stb & 0x08:
                    reason = "SMU output shut down to protect the SMU"
                elif stb & 0x20:
                    reason = "SMU output reset by a power failure"
                else:
                    reason = "unknown"
                raise RuntimeError("4145B emergency: %s (status byte %d)" % (reason, stb))
            if stb & 0x08:
                raise RuntimeError("4145B rejected the setup (Illegal Program, status byte %d); "
                                   "check the CRT for the error message" % stb)
            if stb & 0x02:
                print("/!\\ 4145B reported a syntax error in a program code (status byte %d)" % stb)
            if stb & 0x01:
                return
            if time.time() - t0 > limit:
                raise TimeoutError("4145B did not report Data Ready within %.0f s (last status byte %d)"
                                   % (limit, stb))
            time.sleep(0.05)

    def _read_channel(self, name):
        raw = self.pyvisa_resource.query("DO '%s'" % name).strip()
        fields = [f.strip() for f in raw.split(",") if f.strip()]
        status = np.array([f[0] for f in fields])
        values = np.array([float(f[1:]) for f in fields])
        return values, status

    # -------------------------------------------------------------- measurement
    def launch_measurements(self, activate_segmentation=False):
        """ Launch the measurement and return the data (array) and the header (string)

            args:
                - activate_segmentation (boolean) : if True, the sweep is split in smaller sweeps
                  (50/30/10 points for S/M/L integration). Not needed now that Data Ready is polled;
                  the 4145B handles up to 512 points per sweep.
            return:
                - data (array) : column 0 = programmed VAR1 values, then one column per measured SMU
                - header (string) : description of the columns, e.g. "V1, I1"
            Per-point status letters are stored in self.last_status (same shape as data[:, 1:]).
        """
        names = self._measured_names()
        sweep_list = self._sweep_list()
        n_v = len(sweep_list)
        data = np.zeros((n_v, len(names) + 1))
        status = np.full((n_v, len(names)), "?", dtype="<U1")
        data[:, 0] = sweep_list

        if activate_segmentation and self.sweep_type == "linear":
            n_max = {"S": 50, "M": 30, "L": 10}[self.integration_mode]
        else:
            n_max = n_v
        step = self._signed_step()

        for first in range(0, n_v, n_max):
            last = min(first + n_max, n_v) - 1
            if n_max != n_v:
                self.pyvisa_resource.write("SS")
                self._write_var1(sweep_list[first], sweep_list[last], step)
            self.pyvisa_resource.write("BC")
            self.pyvisa_resource.read_stb()  # clear pending status bits
            self.pyvisa_resource.write("MD")
            self.pyvisa_resource.write("ME1")  # single measurement
            self._wait_data_ready(last - first + 1)
            for col, name in enumerate(names, start=1):
                values, st = self._read_channel(name)
                if len(values) != last - first + 1:
                    raise RuntimeError("Expected %d points for %s, received %d"
                                       % (last - first + 1, name, len(values)))
                data[first:last + 1, col] = values
                status[first:last + 1, col - 1] = st

        self.last_status = status
        flagged = status != "N"
        if flagged.any():
            for col, name in enumerate(names):
                letters = sorted(set(status[flagged[:, col], col]))
                if letters:
                    print("/!\\ %s: %d point(s) flagged %s" % (
                        name, int(flagged[:, col].sum()),
                        ", ".join("%s (%s)" % (l, self.STATUS_MEANING.get(l, "unknown")) for l in letters)))

        master_type = self.smu_type["SMU%d" % self.smu_master]
        header = "%s%d" % ("V" if master_type == "voltage" else "I", self.smu_master)
        header += "".join(", %s" % name for name in names)
        return data, header
