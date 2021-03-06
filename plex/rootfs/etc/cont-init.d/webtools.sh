#!/usr/bin/with-contenv bashio
# ==============================================================================
# Home Assistant Community Add-on: Plex Media Server
# Enables the WebTools plugin if the user requested that
# ==============================================================================

bashio::log.info 'WebTools...'

if bashio::config.true 'webtools' && ! bashio::fs.directory_exists \
        "$(bashio::config 'Config_Folder')/Plug-ins/WebTools.bundle"; then
    bashio::log.info 'Enabling WebTools plugin...'
    mkdir -p "$(bashio::config 'Config_Folder')/Plex Media Server/Plug-ins/"
    ln -s "/opt/WebTools.bundle" "/data/Plex Media Server/Plug-ins/"
fi

if bashio::config.false 'webtools' && bashio::fs.directory_exists \
        "$(bashio::config 'Config_Folder')/Plug-ins/WebTools.bundle"; then
    rm -f "$(bashio::config 'Config_Folder')/Plug-ins/WebTools.bundle"
fi
