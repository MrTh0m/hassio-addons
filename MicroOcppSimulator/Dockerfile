ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# Outils nécessaires pour compiler MicroOcppSimulator depuis les sources
# (mêmes dépendances que celles utilisées par le Dockerfile officiel du projet)
RUN apk add --no-cache \
        git \
        cmake \
        openssl-dev \
        build-base \
        bash

WORKDIR /opt/microocppsimulator

# Référence git à cloner (branche, tag ou commit). Modifiable via --build-arg
# pour figer une version précise plutôt que de suivre "main".
ARG SIMULATOR_REF=main

# Récupère les sources officielles du simulateur, avec ses submodules
RUN git clone --recurse-submodules --branch ${SIMULATOR_REF} --depth 1 \
        https://github.com/matth-x/MicroOcppSimulator . \
    && rm -rf .git

# Compilation (identique à la procédure documentée par le projet)
RUN cmake -S . -B ./build \
    && cmake --build ./build -j "$(nproc)" --target mo_simulator \
    && chmod +x ./build/mo_simulator

# Le simulateur sert son interface web sur le port 8000
EXPOSE 8000

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
