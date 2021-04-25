ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY root/ /

# Labels
LABEL \
    io.hass.name="Plex Media Server" \
    io.hass.description="Recorded media, live TV, online news, and podcasts ready to stream." \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="" \
    org.label-schema.description="Recorded media, live TV, online news, and podcasts ready to stream." \
    org.label-schema.build-date=${BUILD_DATE} \
    org.label-schema.name="Plex Media Server" \
    org.label-schema.schema-version="1.0" \
    org.label-schema.url="https://github.com/MrTh0m/addon-plex/" \
    org.label-schema.usage="https://github.com/MrTh0m/addon-plex/blob/master/README.md" \
    org.label-schema.vcs-ref=${BUILD_REF} \
    org.label-schema.vcs-url="https://github.com/hassio-addons/addon-plex" \
    org.label-schema.vendor="Home Assistant Community Add-ons"
