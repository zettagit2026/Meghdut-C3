# cema-rf-bridge — Activation Runbook

`cema-rf-bridge.service` runs `rf-bridge/mavlink_bridge.py`, the combined
**RX + TX** MAVLink bridge. It is the ONLY component that writes frames to the
radio and it drives the sustained RC-override takeover. It is intentionally
staged **DISABLED + STOPPED** so it neither auto-starts on boot nor
crash-loops while no TX radio is attached.

Do NOT start it until BOTH are true:
1. A MAVLink TX radio is physically connected AND presents as `/dev/cema-sik-adapter`.
2. A human has decided to arm the transmit host.

Activation does NOT itself transmit: `mavlink_bridge.py` still fails closed on
the live `GET /api/range-authorization/status?effect=mavlink` lease and honors
`tx_halted`/EMERGENCY-ABORT. Starting the service only makes the RX+TX path
*available*; an operator must still arm range-authorization from the app.

## Prerequisite checks (run before activating)

```bash
# 1. Radio present as the stable udev symlink (NOT a raw /dev/ttyUSB0):
ls -l /dev/cema-sik-adapter        # must resolve to the ttyUSB of the SiK radio

# 2. The interpreter the unit uses can import the deps:
/CEMA/joydipdemo/field-bridge/.venv/bin/python3 -c "import serial, pymavlink, websocket; print('deps OK')"

# 3. Env file present and locked down:
ls -l /CEMA/joydipdemo/rf-bridge/.env   # -rw------- (chmod 600)
```

## CRITICAL prerequisite — release the serial port from the sniffer

`cema-rf-bridge` (RX+TX) and `cema-mavlink-sniffer` (passive RX only) both want
the SINGLE SiK radio. A serial port can be opened by only one process. The
RX+TX bridge SUBSUMES the sniffer's passive-RX role on the transmit host, so
**stop the sniffer first** or the two will contend for the port:

```bash
sudo systemctl stop cema-mavlink-sniffer.service
```

(Leave it stopped while rf-bridge owns the radio. Optionally
`sudo systemctl disable cema-mavlink-sniffer.service` if this host is now a
dedicated transmit host, so it does not restart on boot and re-grab the port.)

## One-command activation (tomorrow)

```bash
sudo systemctl enable --now cema-rf-bridge.service
```

Then verify:

```bash
systemctl is-enabled cema-rf-bridge.service   # -> enabled
systemctl is-active  cema-rf-bridge.service   # -> active (running)
tail -f /CEMA/joydipdemo/rf-bridge/mavlink_bridge.log
```

## Rollback / re-stage dormant

```bash
sudo systemctl disable --now cema-rf-bridge.service
sudo systemctl start cema-mavlink-sniffer.service   # hand the radio back to passive RX
```

## Gotcha — the incoming radio must match the udev rule

`/dev/cema-sik-adapter` is created by `/etc/udev/rules.d/99-cema-sik-adapter.rules`:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", ATTRS{serial}=="0001", SYMLINK+="cema-sik-adapter"
```

This matches a **CP210x (Silicon Labs 10c4:ea60) SiK adapter with serial "0001"**.
If tomorrow's radio is a different chip (e.g. an FTDI RFD900 = 0403:xxxx) OR the
same CP210x with a different `serial`, the symlink will NOT appear and the
service will fail to open the port. Fix by adding a matching udev rule for the
new device, e.g. read its ids with `udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|idProduct|serial'`,
add a `SYMLINK+="cema-sik-adapter"` line, then `sudo udevadm control --reload &&
sudo udevadm trigger`, and re-check `ls -l /dev/cema-sik-adapter` before activating.
