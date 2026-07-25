# TLS Enforcement — Current State (OWASP gap #87)

## Finding: TLS is NOT present anywhere in this stack today

Investigated 2026-07-25 across `docker-compose.yml`, `frontend/docker/nginx.conf`,
and `backend/server.py`. Plainly stated:

- **Frontend container (nginx)**: `frontend/docker/nginx.conf` has a single
  `server` block that `listen`s on plain port **3000**. There is no `listen 443
  ssl`, no `ssl_certificate`/`ssl_certificate_key` directive, and no redirect
  from HTTP to HTTPS. It reverse-proxies `/api/` and `/api/ws/` to the backend
  over plain HTTP on the internal docker network.
- **Backend (FastAPI/uvicorn)**: `backend/server.py` has no
  `HTTPSRedirectMiddleware`, no HSTS header, and no ASGI-layer TLS termination.
  It's served over plain HTTP on port 8001.
- **docker-compose.yml**: exposes `frontend` on `${BIND_HOST:-0.0.0.0}:3000` and
  `backend` on `${BIND_HOST:-0.0.0.0}:8001`, both plain HTTP, both bound to all
  interfaces by default (only `mongo` is restricted to localhost). No reverse
  proxy, load balancer, or sidecar in this repo terminates TLS anywhere.
- `CORS_ORIGINS` default (`http://localhost:3000`) is consistent with this: the
  whole stack assumes HTTP end-to-end today.

**Conclusion: every hop — browser to nginx, nginx to backend, and any
LAN/WAN access to the exposed ports — is unencrypted HTTP.** This means JWTs
(bearer tokens for operator sessions), login credentials, and live detection/
CEMA mission data all cross the wire in cleartext. On a shared network segment
this is sniffable and JWTs are stealable via passive capture, not just active
MITM.

## Why this isn't being fixed in this pass

Adding HSTS headers or an `HTTPSRedirectMiddleware` right now would be
actively misleading: HSTS/redirect-to-HTTPS only make sense once a real TLS
listener exists to redirect *to*. Enabling either without real TLS in front
would either break the app (if enforced) or be a no-op that falsely signals
"TLS is handled" (if left permissive). This repo also has no certificate
material, no CA/ACME integration, and the deployment topology (bare
docker-compose across LAN IPs and hostnames, `frontend`/`backend` addressed by
service name inside the docker network) is not yet decided in a way that a
correct TLS setup (which certs, which SANs, mTLS between services or just
edge TLS, cert renewal automation) can be safely picked without infra
decisions and human sign-off — consistent with the explicit instruction not
to half-implement TLS or restructure deployment topology without that
sign-off.

## Recommendation — track as its own infra task

This should be a dedicated infrastructure task, not a quick code patch:

1. **Decide the TLS boundary.** Most likely: terminate TLS at the nginx
   (frontend) container or in front of it (a dedicated reverse proxy /
   load balancer), with real certificates for whatever hostname(s)/IP(s) the
   deploy actually uses (e.g. Caddy/Traefik with ACME if a public/resolvable
   name is available, or an internally-issued cert + distributed trust anchor
   for LAN-only deployments where public ACME can't reach the host).
2. **Add `listen 443 ssl` + cert paths to `frontend/docker/nginx.conf`**, and a
   `server { listen 80; return 301 https://$host$request_uri; }` block for the
   HTTP->HTTPS redirect, once certs exist.
3. **Add HSTS** (`Strict-Transport-Security: max-age=...; includeSubDomains`)
   at that point — not before.
4. **Update `docker-compose.yml`** to mount cert/key material into the
   frontend container and expose 443 instead of (or alongside, during
   migration) 3000.
5. **Update `CORS_ORIGINS`** and all deploy docs to `https://` once live.
6. Given this platform's stated OSI-permissive/open-source-sovereignty
   requirements, prefer open tooling for cert issuance/renewal (e.g. Caddy's
   built-in ACME, or OpenBao-managed internal PKI for LAN-only/air-gapped
   deployments) over closed/commercial cert management.

None of the above is implemented in this change — this note is the honest
finding plus the explicit flag that it needs its own dedicated, human-approved
infra task before touching the live deployment topology or provisioning
certificates.
