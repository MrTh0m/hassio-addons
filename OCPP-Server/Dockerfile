# Home Assistant a retiré la lecture de build.json depuis Supervisor 2026.04.0 :
# l'image de base se fixe directement ici.
FROM alpine:3.20

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="aarch64|amd64|armhf|armv7|i386"

# python3/pip + toolchain de compilation. bcrypt publie des wheels musllinux
# (donc pas de compilation nécessaire sur la plupart des architectures), mais
# cargo/rust est inclus en secours pour les architectures moins courantes
# (armhf, i386) où aucune wheel pré-compilée n'est disponible.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        gcc \
        musl-dev \
        python3-dev \
        libffi-dev \
        openssl-dev \
        cargo

WORKDIR /opt/ocpp-server

COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
