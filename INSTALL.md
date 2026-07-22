# MEGHADUTA C³ — Local Install

One-command local runner for **Windows / Linux / macOS**.
Bundles: MongoDB + FastAPI backend + React frontend (Nginx).

---

## 1. Prerequisites

- **Docker Desktop** (Windows / macOS) or **Docker Engine + docker compose v2** (Linux).
- Ports **3000**, **8001**, **27017** must be free on your machine.

---

## 2. Run it

### Windows
Double-click **`start.bat`**  _(or run it from PowerShell/cmd)_.

### Linux / macOS
```bash
chmod +x start.sh stop.sh
./start.sh
```

That's it. First run takes 2–4 minutes (image build). Later runs boot in seconds.

---

## 3. Open the app

| Service   | URL                              |
|-----------|----------------------------------|
| Frontend  | http://localhost:3000            |
| Backend   | http://localhost:8001/api/       |
| Mongo     | mongodb://localhost:27017        |

Default operator login (also auto-seeded on first boot):

- Email:    `operator@cema.mil`
- Password: `cema@2026`

You can override these before first boot by setting env vars, e.g.:

```bash
JWT_SECRET="<64-hex>" ADMIN_EMAIL="me@lab" ADMIN_PASSWORD="strongpass" ./start.sh
```

---

## 4. Stop / uninstall

```bash
./stop.sh              # or stop.bat on Windows
```

Wipe database (fresh start next boot):
```bash
docker compose down -v
```

Remove Docker images:
```bash
docker rmi $(docker images "cema*" -q)
```

---

## 5. What's inside

```
/app
├── backend/                FastAPI + JWT + MongoDB + WebSocket
│   ├── Dockerfile
│   ├── server.py           routes: auth, detections, mavlink, payloads, logs, ws
│   ├── mavlink_codec.py    byte-accurate MAVLink v1/v2 packet builder
│   ├── payload_library.py  10 cyber-physical payload specs
│   ├── simulator.py        drone + RF waterfall simulator
│   └── requirements.txt
├── frontend/               React 19 + Tailwind + shadcn/ui + Nginx
│   ├── Dockerfile
│   ├── docker/nginx.conf
│   └── src/                Login, Dashboard, Signals, MAVLink, Payloads, KillChain, Log
├── docker-compose.yml      Mongo + Backend + Frontend, wired together
├── start.sh / start.bat    one-command launchers
└── stop.sh / stop.bat      teardown
```

---

## 6. Troubleshooting

- **Port already in use** → stop whatever is on 3000/8001/27017 or edit `docker-compose.yml` ports.
- **Docker not running** → open Docker Desktop and wait for the whale icon to go steady.
- **Frontend can't reach backend** → confirm `REACT_APP_BACKEND_URL` build-arg in `docker-compose.yml` matches how you're accessing the app (default: `http://localhost:8001`).
- **Fresh install after schema change** → `docker compose down -v && ./start.sh`.

---

## 7. Security / operational notice

This is an **evaluation build**. MAVLink packets are byte-accurate (CRC-16/MCRF4XX + CRC_EXTRA) and would be accepted by a real ArduPilot/PX4 receiver — the packets are **not transmitted over RF**, they are emitted only on the internal WebSocket bus at `ws://localhost:8001/api/ws/mavlink`. To go live, connect an SDR TX chain (BladeRF/USRP/HackRF via GNU Radio) that pipes those frames to the air.

**Do not use this build for operational counter-UAS.** RESTRICTED — for MoD-style methodology evaluation only.
