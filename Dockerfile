# lg-hue-sync-deployer
# Toolchain identisch zu adeze/lg-hue-sync docker/Dockerfile.cross
# (Debian Buster, glibc 2.28 = Ceiling des LG C1 / webOS 6, soft-float armel).
FROM debian:buster

ARG RUST_VERSION=stable

ENV RUSTUP_HOME=/opt/rust/rustup \
    PATH=/opt/rust/cargo/bin:${PATH} \
    CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABI_LINKER=arm-linux-gnueabi-gcc \
    CC_armv7_unknown_linux_gnueabi=arm-linux-gnueabi-gcc \
    AR_armv7_unknown_linux_gnueabi=arm-linux-gnueabi-ar \
    RANLIB_armv7_unknown_linux_gnueabi=arm-linux-gnueabi-ranlib \
    HOME=/config \
    CONFIG_DIR=/config \
    WEBUI_PORT=8789 \
    PYTHONUNBUFFERED=1

RUN printf '%s\n' \
      'deb [trusted=yes] http://archive.debian.org/debian buster main' \
      'deb [trusted=yes] http://archive.debian.org/debian-security buster/updates main' \
      > /etc/apt/sources.list \
 && apt-get -o Acquire::Check-Valid-Until=false update -qq \
 && apt-get install --allow-unauthenticated -y -qq --no-install-recommends \
      build-essential gcc-arm-linux-gnueabi libc6-dev-armel-cross \
      binutils-arm-linux-gnueabi make perl curl ca-certificates \
      git openssh-client sshpass netcat-openbsd python3 tini \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/rust/cargo /opt/rust/rustup \
 && CARGO_HOME=/opt/rust/cargo curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | CARGO_HOME=/opt/rust/cargo sh -s -- -y --no-modify-path \
        --default-toolchain "${RUST_VERSION}" --profile minimal \
        --target armv7-unknown-linux-gnueabi \
 && rustc --version && cargo --version

COPY app /app

EXPOSE 8789
VOLUME /config

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD curl -fsS "http://127.0.0.1:${WEBUI_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/server.py"]
