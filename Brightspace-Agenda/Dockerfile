# syntax=docker/dockerfile:1
#
# Brightspace Agenda - addon Home Assistant
# Convention 2026 (post-migration BuildKit) : pas de build.yaml, base image et
# labels directement dans ce Dockerfile. Voir
# https://developers.home-assistant.io/blog/2026/04/02/builder-migration

FROM php:8.2-apache

ARG BUILD_VERSION
ARG BUILD_ARCH
# Reference du depot public a recuperer au build (branche, tag ou commit).
# Pour une version d'addon "figee" et reproductible, epingler un tag/commit
# plutot que la branche de developpement dev.
ARG BSA_REF=dev

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Brightspace Agenda" \
    org.opencontainers.image.description="Suivi des devoirs, live sessions et ateliers Brightspace by D2L, auto-heberge" \
    org.opencontainers.image.source="https://github.com/MrTh0m/Brightspace_agenda" \
    org.opencontainers.image.licenses="MIT"

# ── Extensions PHP requises par api.php/proxy.php (curlFetch) ──────────────
RUN docker-php-ext-install curl \
    && a2enmod rewrite headers

# ── Recuperation de la webapp depuis le depot public ────────────────────────
# Pas de copie locale dans ce depot d'addon : evite un 4e point de
# synchronisation en plus des 3 deja existants (web, APK www/, APK assets/public/).
COPY rootfs-build/patch-ingress-cookie.sh /usr/local/bin/patch-ingress-cookie.sh
RUN chmod a+x /usr/local/bin/patch-ingress-cookie.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/www/html/* \
    && git clone --depth 1 --branch "${BSA_REF}" https://github.com/MrTh0m/Brightspace_agenda.git /tmp/bsa \
    && cp /tmp/bsa/index.html /tmp/bsa/api.php /tmp/bsa/proxy.php /tmp/bsa/setup.php \
          /tmp/bsa/manifest.json /tmp/bsa/sw.js /var/www/html/ \
    && (cp /tmp/bsa/icon-192.png /tmp/bsa/icon-512.png /tmp/bsa/apple-touch-icon.png /var/www/html/ 2>/dev/null || true) \
    && /usr/local/bin/patch-ingress-cookie.sh \
    && rm -rf /tmp/bsa /usr/local/bin/patch-ingress-cookie.sh \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

# ── Apache : port Ingress ───────────────────────────────────────────────────
RUN grep -q "^Listen 80$" /etc/apache2/ports.conf \
    && sed -i 's/^Listen 80$/Listen 8099/' /etc/apache2/ports.conf \
    && grep -q "<VirtualHost \*:80>" /etc/apache2/sites-available/000-default.conf \
    && sed -i 's/<VirtualHost \*:80>/<VirtualHost *:8099>/' /etc/apache2/sites-available/000-default.conf

# ── Apache : .htaccess de l'app (protection data/) ──────────────────────────
# Fichier de conf dedie plutot qu'un sed sur apache2.conf : voir le
# commentaire dans bsa-allowoverride.conf pour le detail du risque evite.
COPY rootfs-build/bsa-allowoverride.conf /etc/apache2/conf-available/bsa-allowoverride.conf
RUN a2enconf bsa-allowoverride

# ── Bootstrap runtime (fuseau horaire + provisionnement du mot de passe) ───
COPY rootfs-run/bsa-bootstrap.php /usr/local/bin/bsa-bootstrap.php

COPY run.sh /
RUN chmod a+x /run.sh

EXPOSE 8099

CMD [ "/run.sh" ]
