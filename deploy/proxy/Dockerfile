FROM caddy:2.8.4-alpine@sha256:af32e97399febea808609119bb21544d0265c58a02836576e32a2d082c262c17

# The upstream binary carries cap_net_bind_service. Docker refuses to exec a
# file-capability binary under no-new-privileges after all capabilities are dropped,
# even though this proxy only listens on unprivileged port 18443. Copying the binary
# removes that unnecessary extended attribute while retaining the locked-down runtime.
RUN cp /usr/bin/caddy /usr/bin/caddy-unprivileged \
    && chmod 0755 /usr/bin/caddy-unprivileged \
    && mv /usr/bin/caddy-unprivileged /usr/bin/caddy
