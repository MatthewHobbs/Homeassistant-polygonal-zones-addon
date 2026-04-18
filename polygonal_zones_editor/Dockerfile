ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir --upgrade pip

# Install pinned, hash-verified deps from the generated lock file.
# requirements.txt remains the high-level spec; regenerate the lock with
#   uv pip compile requirements.txt --generate-hashes \
#     --python-version 3.11 -o requirements-lock.txt
# The --python-version must match the python3 shipped by the HA base image
# (Alpine 3.19 → Python 3.11) so transitive deps like typing_extensions
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
