#!/usr/bin/with-contenv bashio
# ==============================================================================
# Home Assistant Community Add-on: Plex Media Server
# Enables the WebTools plugin if the user requested that
# ==============================================================================
Config_Folder=$(bashio::config 'Config_Folder')

if bashio::config.true 'webtools' && ! bashio::fs.directory_exists \
        "$(Config_Folder)/Plug-ins/WebTools.bundle"; then
    bashio::log.info 'Enabling WebTools plugin...'
    mkdir -p "$(Config_Folder)/Plex Media Server/Plug-ins/"
    ln -s "/opt/WebTools.bundle" "/data/Plex Media Server/Plug-ins/"
fi

if bashio::config.false 'webtools' && bashio::fs.directory_exists \
        "$(Config_Folder)/Plex Media Server/Plug-ins/WebTools.bundle"; then
    rm -f "$(Config_Folder)/Plex Media Server/Plug-ins/WebTools.bundle"
fi
