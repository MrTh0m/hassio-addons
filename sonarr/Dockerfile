ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV XDG_CONFIG_HOME="/data/xdg"

COPY root/ /

ARG \
    BASHIO_VERSION

WORKDIR /usr/src

RUN \
	echo "**** Install BASHIO ****" && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        curl &&\
	mkdir -p /usr/src/bashio &&\
    curl -L -f -s "https://github.com/hassio-addons/bashio/archive/v${BASHIO_VERSION}.tar.gz" \
        | tar -xzf - --strip 1 -C /usr/src/bashio &&\
    mv /usr/src/bashio/lib /usr/lib/bashio &&\
    ln -s /usr/lib/bashio/bashio /usr/bin/bashio