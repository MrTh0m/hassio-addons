# syntax=docker/dockerfile:1
#
# Brightspace Agenda - addon Home Assistant
# Convention 2026 (post-migration BuildKit) : pas de build.yaml, base image et
# labels directement dans ce Dockerfile. Voir
# https://developers.home-assistant.io/blog/2026/04/02/builder-migration

FROM php:8.2-apache

ARG BUILD_VERSION
ARG BUILD_ARCH
# Référence du dépôt public à récupérer au build (branche, tag ou commit).
# main = branche par défaut/stable sur GitHub (vérifié via origin/HEAD).
# dev est la branche de travail active (potentiellement instable à tout moment) :
# à ne jamais utiliser pour un build d'addon public.
# Pour une version d'addon "figée" et reproductible, épingler un tag/commit
# plutôt que main directement.
ARG BSA_REF=main

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Brightspace Agenda" \
    org.opencontainers.image.description="Suivi des devoirs, live sessions et ateliers Brightspace by D2L, auto-hébergé" \
    org.opencontainers.image.source="https://github.com/MrTh0m/Brightspace_agenda" \
    org.opencontainers.image.licenses="MIT"

# BSA_DATA_DIR : lu par api.php/setup.php (patch-data-dir-env.sh) pour que
# DATA_DIR pointe directement sur /data, le volume persistant du Supervisor,
# sans passer par un lien symbolique depuis /var/www/html. Voir le
# commentaire de patch-data-dir-env.sh pour le detail (confinement AppArmor
# des addons, qui bloque la traversee d'un lien symbolique entre la couche
# d'image du conteneur et un volume monte separement, meme avec des
# permissions Unix par ailleurs correctes de bout en bout).
ENV BSA_DATA_DIR=/data

# Sessions PHP dans /data plutot que /tmp (par defaut) : /tmp fait partie de
# la couche ecriture du conteneur, effacee a chaque redemarrage/rebuild, ce
# qui deconnecterait silencieusement tout le monde en mode connecte a chaque
# mise a jour de l'addon ou reboot de l'hote Home Assistant. Le dossier lui
# meme est cree au runtime par run.sh (n'existe pas encore au moment du
# build, /data n'etant monte qu'a l'execution).
RUN echo "session.save_path = /data/sessions" > /usr/local/etc/php/conf.d/bsa-sessions.ini

# ── Extensions PHP requises par api.php/proxy.php (curlFetch) + curl CLI ───
# docker-php-ext-install compile l'extension depuis les sources : il faut les
# en-têtes de dev de libcurl (absentes de l'image de base), pas seulement la
# lib runtime. On ne purge pas libcurl4-openssl-dev après coup : l'extension
# compilée en a besoin aussi à l'exécution (risque de casse au démarrage
# sinon), le gain de taille ne vaut pas ce risque.
# Le paquet curl (binaire CLI, distinct de libcurl4-openssl-dev et de
# l'extension PHP) est nécessaire pour bsa-publish-discovery.sh, qui appelle
# curl directement en shell, pas depuis du code PHP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4-openssl-dev curl \
    && docker-php-ext-install curl \
    && a2enmod rewrite headers \
    && rm -rf /var/lib/apt/lists/*

# ── Récupération de la webapp depuis le dépôt public ────────────────────────
# Pas de copie locale dans ce dépôt d'addon : évite un 4e point de
# synchronisation en plus des 3 déjà existants (web, APK www/, APK assets/public/).
COPY rootfs-build/patch-ingress-cookie.sh /usr/local/bin/patch-ingress-cookie.sh
COPY rootfs-build/patch-ingress-baseurl.sh /usr/local/bin/patch-ingress-baseurl.sh
COPY rootfs-build/patch-data-dir-env.sh /usr/local/bin/patch-data-dir-env.sh
RUN chmod a+x /usr/local/bin/patch-ingress-cookie.sh /usr/local/bin/patch-ingress-baseurl.sh /usr/local/bin/patch-data-dir-env.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/www/html/* \
    && git clone --depth 1 --branch "${BSA_REF}" https://github.com/MrTh0m/Brightspace_agenda.git /tmp/bsa \
    && cp /tmp/bsa/index.html /tmp/bsa/api.php /tmp/bsa/proxy.php /tmp/bsa/setup.php \
          /tmp/bsa/manifest.json /tmp/bsa/sw.js /var/www/html/ \
    && (cp /tmp/bsa/icon-192.png /tmp/bsa/icon-512.png /tmp/bsa/apple-touch-icon.png /var/www/html/ 2>/dev/null || true) \
    && /usr/local/bin/patch-ingress-cookie.sh \
    && /usr/local/bin/patch-ingress-baseurl.sh \
    && /usr/local/bin/patch-data-dir-env.sh \
    && rm -rf /tmp/bsa /usr/local/bin/patch-ingress-cookie.sh /usr/local/bin/patch-ingress-baseurl.sh /usr/local/bin/patch-data-dir-env.sh \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

# ── Apache : port Ingress ───────────────────────────────────────────────────
RUN grep -q "^Listen 80$" /etc/apache2/ports.conf \
    && sed -i 's/^Listen 80$/Listen 8099/' /etc/apache2/ports.conf \
    && grep -q "<VirtualHost \*:80>" /etc/apache2/sites-available/000-default.conf \
    && sed -i 's/<VirtualHost \*:80>/<VirtualHost *:8099>/' /etc/apache2/sites-available/000-default.conf

# ── Bootstrap runtime (fuseau horaire + provisionnement du mot de passe) ───
COPY rootfs-run/bsa-bootstrap.php /usr/local/bin/bsa-bootstrap.php

# ── Publication Discovery Supervisor (détection auto par l'intégration HACS) ─
COPY rootfs-run/bsa-publish-discovery.sh /usr/local/bin/bsa-publish-discovery.sh
RUN chmod a+x /usr/local/bin/bsa-publish-discovery.sh

COPY run.sh /
RUN chmod a+x /run.sh

EXPOSE 8099

CMD [ "/run.sh" ]
