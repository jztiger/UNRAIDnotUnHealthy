# syntax=docker/dockerfile:1.7
#
# UNRAIDnotUnHealthy — single-container Prometheus + Grafana + exporters.
#
# Bump these to upgrade. CI should rebuild on changes.
ARG DEBIAN_TAG=bookworm-slim
ARG S6_OVERLAY_VERSION=3.2.0.2
ARG PROMETHEUS_VERSION=3.1.0
ARG GRAFANA_VERSION=11.4.0
ARG NODE_EXPORTER_VERSION=1.8.2
ARG CADVISOR_VERSION=0.49.1
ARG SMARTCTL_EXPORTER_VERSION=0.13.0
ARG IPMI_EXPORTER_VERSION=1.9.0
ARG NVIDIA_GPU_EXPORTER_VERSION=1.2.1
ARG LOKI_VERSION=3.3.0
ARG ALLOY_VERSION=1.5.0
ARG EXPORTARR_VERSION=2.3.0

# ---------------------------------------------------------------------------
# Stage 1: download all upstream artefacts in one cacheable layer
# ---------------------------------------------------------------------------
FROM debian:${DEBIAN_TAG} AS downloader
ARG S6_OVERLAY_VERSION
ARG PROMETHEUS_VERSION
ARG GRAFANA_VERSION
ARG NODE_EXPORTER_VERSION
ARG CADVISOR_VERSION
ARG SMARTCTL_EXPORTER_VERSION
ARG IPMI_EXPORTER_VERSION
ARG NVIDIA_GPU_EXPORTER_VERSION
ARG LOKI_VERSION
ARG ALLOY_VERSION
ARG EXPORTARR_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tar xz-utils unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /dl

# s6-overlay (amd64 only — Unraid is amd64)
RUN curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -o s6-noarch.tar.xz \
 && curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz" -o s6-x86_64.tar.xz

# Prometheus
RUN curl -fsSL "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz" | tar xz \
 && mv "prometheus-${PROMETHEUS_VERSION}.linux-amd64" prometheus

# node_exporter
RUN curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz" | tar xz \
 && mv "node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64" node_exporter

# smartctl_exporter
RUN curl -fsSL "https://github.com/prometheus-community/smartctl_exporter/releases/download/v${SMARTCTL_EXPORTER_VERSION}/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-amd64.tar.gz" | tar xz \
 && mv "smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-amd64" smartctl_exporter

# ipmi_exporter
RUN curl -fsSL "https://github.com/prometheus-community/ipmi_exporter/releases/download/v${IPMI_EXPORTER_VERSION}/ipmi_exporter-${IPMI_EXPORTER_VERSION}.linux-amd64.tar.gz" | tar xz \
 && mv "ipmi_exporter-${IPMI_EXPORTER_VERSION}.linux-amd64" ipmi_exporter

# nvidia_gpu_exporter (utkuozdemir's wrapper around nvidia-smi)
RUN mkdir nvidia_gpu_exporter \
 && curl -fsSL "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${NVIDIA_GPU_EXPORTER_VERSION}/nvidia_gpu_exporter_${NVIDIA_GPU_EXPORTER_VERSION}_linux_x86_64.tar.gz" \
   | tar xz -C nvidia_gpu_exporter

# cadvisor (single static binary)
RUN curl -fsSL "https://github.com/google/cadvisor/releases/download/v${CADVISOR_VERSION}/cadvisor-v${CADVISOR_VERSION}-linux-amd64" -o cadvisor \
 && chmod +x cadvisor

# Grafana OSS
RUN curl -fsSL "https://dl.grafana.com/oss/release/grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz" | tar xz \
 && mv "grafana-v${GRAFANA_VERSION}" grafana

# Loki (zip → single binary)
RUN curl -fsSL -o loki.zip "https://github.com/grafana/loki/releases/download/v${LOKI_VERSION}/loki-linux-amd64.zip" \
 && unzip -q loki.zip \
 && rm loki.zip \
 && mv loki-linux-amd64 loki \
 && chmod +x loki

# Alloy (zip → single binary)
RUN curl -fsSL -o alloy.zip "https://github.com/grafana/alloy/releases/download/v${ALLOY_VERSION}/alloy-linux-amd64.zip" \
 && unzip -q alloy.zip \
 && rm alloy.zip \
 && mv alloy-linux-amd64 alloy \
 && chmod +x alloy

# exportarr (Sonarr/Radarr/etc Prometheus exporter). Tarball nests into
# exportarr_<v>_linux_amd64/ — strip that to leave the binary at /dl/exportarr.
RUN curl -fsSL "https://github.com/onedr0p/exportarr/releases/download/v${EXPORTARR_VERSION}/exportarr_${EXPORTARR_VERSION}_linux_amd64.tar.gz" \
    | tar xz --strip-components=1 \
 && chmod +x exportarr

# ---------------------------------------------------------------------------
# Stage 2: final image
# ---------------------------------------------------------------------------
FROM debian:${DEBIAN_TAG}

# Version stamping — populated by scripts/docker-build.sh from git.
# Defaults keep `docker build` working without the wrapper script.
ARG BUILD_COMMIT=unknown
ARG BUILD_COMMIT_COUNT=0
ARG BUILD_TIME=unknown
ARG GRAFANA_VERSION

LABEL org.opencontainers.image.title="UNRAIDnotUnHealthy" \
      org.opencontainers.image.description="Single-container Prometheus + Grafana monitoring stack for Unraid." \
      org.opencontainers.image.source="https://github.com/jztiger/UNRAIDnotUnHealthy" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.unhealthy.build.commit="${BUILD_COMMIT}" \
      org.unhealthy.build.commit_count="${BUILD_COMMIT_COUNT}" \
      org.unhealthy.build.time="${BUILD_TIME}"

ENV BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_COMMIT_COUNT=${BUILD_COMMIT_COUNT} \
    BUILD_TIME=${BUILD_TIME} \
    GF_PATHS_HOME=/usr/share/grafana \
    GF_PATHS_DATA=/var/lib/grafana \
    GF_PATHS_LOGS=/var/log/grafana \
    GF_PATHS_PLUGINS=/var/lib/grafana/plugins \
    GF_PATHS_PROVISIONING=/etc/grafana/provisioning \
    GF_PATHS_CONFIG=/etc/grafana/grafana.ini \
    PROMETHEUS_STORAGE_PATH=/var/lib/prometheus \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      tzdata \
      smartmontools \
      freeipmi-tools \
      libfontconfig1 \
      fonts-dejavu-core \
      bash \
      curl \
      xz-utils \
      python3-minimal \
    && rm -rf /var/lib/apt/lists/*

# Install s6-overlay
COPY --from=downloader /dl/s6-noarch.tar.xz /tmp/
COPY --from=downloader /dl/s6-x86_64.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
 && tar -C / -Jxpf /tmp/s6-x86_64.tar.xz \
 && rm /tmp/s6-*.tar.xz

# Binaries
COPY --from=downloader /dl/prometheus/prometheus           /usr/local/bin/prometheus
COPY --from=downloader /dl/prometheus/promtool             /usr/local/bin/promtool
COPY --from=downloader /dl/node_exporter/node_exporter     /usr/local/bin/node_exporter
COPY --from=downloader /dl/smartctl_exporter/smartctl_exporter /usr/local/bin/smartctl_exporter
COPY --from=downloader /dl/ipmi_exporter/ipmi_exporter     /usr/local/bin/ipmi_exporter
COPY --from=downloader /dl/nvidia_gpu_exporter/nvidia_gpu_exporter /usr/local/bin/nvidia_gpu_exporter
COPY --from=downloader /dl/cadvisor                        /usr/local/bin/cadvisor
COPY --from=downloader /dl/loki                             /usr/local/bin/loki
COPY --from=downloader /dl/alloy                            /usr/local/bin/alloy
COPY --from=downloader /dl/exportarr                        /usr/local/bin/exportarr

# Grafana
COPY --from=downloader /dl/grafana                         /usr/share/grafana

RUN useradd --system --no-create-home --shell /usr/sbin/nologin unhealthy \
 && mkdir -p /var/lib/prometheus /var/lib/grafana /var/log/grafana \
             /var/lib/loki /var/lib/alloy \
             /etc/grafana /etc/prometheus /etc/ipmi_exporter /etc/smartctl_exporter \
             /etc/loki /etc/alloy \
 && chown -R unhealthy:unhealthy /var/lib/prometheus /var/lib/grafana /var/log/grafana \
                                  /var/lib/loki /var/lib/alloy \
 && ln -sf /usr/share/grafana/bin/grafana /usr/local/bin/grafana \
 && ln -sf /usr/share/grafana/bin/grafana-server /usr/local/bin/grafana-server

# Grafana plugins. Install via GF_INSTALL_PLUGINS at startup rather than
# baking with `grafana cli` at build time — /var/lib/grafana is a persistent
# named volume in production, which masks anything baked into that path. The
# env var triggers Grafana's own install-on-start, which writes into the
# (live) volume, persists across recreates, and is a no-op once installed.
# frser-sqlite-datasource powers the Plex Media Analysis dashboard.
ENV GF_INSTALL_PLUGINS=frser-sqlite-datasource

# Bake configs, s6 services, and provisioning into the image
COPY rootfs/                       /
COPY grafana/provisioning/         /etc/grafana/provisioning/
# Keep dashboards outside the grafana-data volume path so rebuilds actually
# update them. The provisioning yml points at /etc/grafana/dashboards.
COPY grafana/dashboards/           /etc/grafana/dashboards/

# s6 longrun scripts and cont-init scripts must be executable
RUN find /etc/s6-overlay/s6-rc.d -name run -exec chmod +x {} + \
 && chmod +x /etc/cont-init.d/* 2>/dev/null || true

# Ship Grafana's stock grafana.ini if a user override isn't provided
RUN cp -n /usr/share/grafana/conf/defaults.ini /etc/grafana/grafana.ini

EXPOSE 3000
VOLUME ["/var/lib/prometheus", "/var/lib/grafana", "/var/lib/loki"]

ENTRYPOINT ["/init"]
