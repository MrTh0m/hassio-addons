ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Git + toolchain C++ pour compiler MicroOcppSimulator, + Node/npm pour reconstruire
# le dashboard web avec une URL d'API relative (voir plus bas).
RUN apk add --no-cache \
        git \
        cmake \
        openssl-dev \
        build-base \
        bash \
        nodejs \
        npm

WORKDIR /opt/microocppsimulator

# Référence git à cloner (branche, tag ou commit).
ARG SIMULATOR_REF=main

# Nombre de connecteurs simulés. Le code source ne gère que deux cas :
# 3 => 2 connecteurs (id 1 et 2), toute autre valeur => 1 connecteur (id 1).
# Ce n'est pas un compteur libre, juste un choix binaire.
ARG MO_NUMCONNECTORS=3

# Sources officielles + submodules (dont le dashboard web dans webapp-src)
RUN git clone --recurse-submodules --branch ${SIMULATOR_REF} --depth 1 \
        https://github.com/matth-x/MicroOcppSimulator . \
    && rm -rf .git

# Applique le nombre de connecteurs choisi
RUN sed -i "s/MO_NUMCONNECTORS=3/MO_NUMCONNECTORS=${MO_NUMCONNECTORS}/" CMakeLists.txt

# --- Fix : reconstruction du dashboard web avec une URL d'API relative ---
# Par défaut le dashboard est livré pré-compilé avec l'URL d'API figée en dur sur
# "http://localhost:8000/api", ce qui casse tout accès via une IP/nom d'hôte différent
# de "localhost" (voir matth-x/MicroOcppSimulator#5). On la reconstruit avec API_ROOT=api
# (chemin relatif), qui fonctionne quel que soit l'hôte utilisé pour y accéder.
RUN cd webapp-src \
    && printf "API_ROOT=api\nNODE_ENV=production\n" > .env.production \
    && npm install --no-audit --no-fund \
    && npm run build \
    && npm run compress \
    && cp dist/bundle.html ../public/bundle.html

# La page est servie non compressée (voir patch ci-dessous), le fichier .gz
# pré-livré par le dépôt n'est plus utilisé.
RUN rm -f public/bundle.html.gz

# --- Fix : sert bundle.html sans revendiquer Content-Encoding: gzip ---
# Le serveur intégré sert par défaut bundle.html.gz avec un header
# "Content-Encoding: gzip" ajouté à la main. Ce header ne survit pas correctement
# au passage par le proxy d'ingress de Home Assistant (la page se télécharge au lieu
# de s'afficher). En servant le bundle.html non compressé, sans revendiquer un
# encodage, on élimine ce problème à la source (le fichier ne fait que ~170 Ko,
# le gain de bande passante du gzip est négligeable dans ce contexte).
RUN sed -i 's/Content-Encoding: gzip\\r\\n//' src/net_mongoose.cpp \
    && sed -i 's/bundle\.html\.gz/bundle.html/' src/net_mongoose.cpp

# --- Fix : compatibilité avec les versions récentes de CMake ---
# lib/mbedtls (dépendance du simulateur) déclare un cmake_minimum_required trop
# ancien. Les versions récentes de CMake refusent purement et simplement de
# configurer un tel projet au lieu d'un simple avertissement. On relève le seuil
# de compatibilité minimal accepté, comme suggéré par le message d'erreur de CMake.
RUN cmake -S . -B ./build -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    && cmake --build ./build -j "$(nproc)" --target mo_simulator \
    && chmod +x ./build/mo_simulator

# Le simulateur sert son interface web sur le port 8000
EXPOSE 8000

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
