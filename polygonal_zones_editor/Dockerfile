# Declared as a global ARG (before the first FROM) so the final stage can still
# reference it after the tempio builder stage below.
ARG BUILD_FROM

# ── tempio builder ───────────────────────────────────────────────────────────
# The stock /usr/bin/tempio in the HA base image is built against golang.org/x/crypto
# 0.26.0 on a stale Go toolchain, so scanners flag it for a batch of CVEs (all in the
# unused crypto/ssh path, plus Go stdlib). Rebuild it on a current Go toolchain with
# x/crypto >= 0.52.0 and overwrite the base copy below. Verified: trivy reports 0 CVEs
# on the result. Tracking upstream fix: https://github.com/home-assistant/tempio/issues/132
# Remove this stage (and the COPY below) once a patched tempio ships in the base image.
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS tempio
# Pinned to an immutable commit for reproducible builds (tempio main @ 2026-06-30 —
# the revision verified to scan clean). Bump deliberately when upstream patches x/crypto.
ARG TEMPIO_REF=5420e16c8cca89903809c842b9925d5fe6778129
ARG TARGETARCH
ARG TARGETVARIANT
RUN apk add --no-cache git
WORKDIR /src
# Fetch the exact commit (git clone --branch can't take a bare SHA), then apply the
# x/crypto security override on top.
RUN git init -q . \
 && git remote add origin https://github.com/home-assistant/tempio \
 && git fetch -q --depth 1 origin "${TEMPIO_REF}" \
 && git checkout -q FETCH_HEAD \
 && go get golang.org/x/crypto@v0.52.0 \
 && go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
    GOARM="$(echo "${TARGETVARIANT}" | tr -d v)" \
    go build -trimpath -ldflags="-s -w" -o /tempio .
# ──────────────────────────────────────────────────────────────────────────────

FROM ${BUILD_FROM}

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Overwrite the base image's vulnerable /usr/bin/tempio with the patched build above.
COPY --from=tempio /tempio /usr/bin/tempio

RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir --upgrade pip

# Install pinned, hash-verified deps from the generated lock file.
# requirements.txt remains the high-level spec; regenerate the lock with
#   uv pip compile requirements.txt --generate-hashes \
#     --python-version 3.12 -o requirements-lock.txt
# The --python-version must match the python3 shipped by the HA base image
# (Alpine 3.21 → Python 3.12) so transitive deps like typing_extensions
# are included even when the maintainer regenerates on a newer host.
COPY requirements-lock.txt /app/requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements-lock.txt

# Non-root runtime user. Permissions on /data are fixed up at boot by
# rootfs/etc/cont-init.d/00-fix-perms so the Supervisor-mounted
# options.json is readable.
RUN addgroup -S app && adduser -S -G app -u 1001 app \
 && chown -R app:app /app

WORKDIR /app
COPY --chown=app:app app /app

# s6-overlay init + service definitions. cont-init.d runs as root to chown
# /data; services.d/web/run drops to the 'app' user via s6-setuidgid.
COPY rootfs /
RUN chmod +x /etc/cont-init.d/00-fix-perms \
             /etc/services.d/web/run \
             /etc/services.d/web/finish

# Build-time sanity: the non-root user and s6-setuidgid must both be in
# place, otherwise the image ships broken and the runtime fallback to root
# would silently kick in on every user.
RUN id app >/dev/null && command -v s6-setuidgid >/dev/null

# No USER directive — s6 must start as root to fix /data permissions;
# the web service itself runs as 'app'.

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8000/healthz || exit 1

# The base image's ENTRYPOINT (/init) runs cont-init.d then services.d.
# No CMD needed.
