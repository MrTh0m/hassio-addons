<?php
/**
 * bsa-bootstrap.php
 *
 * Execute une fois par demarrage du conteneur (voir run.sh), avant Apache.
 * Lit /data/options.json (config de l'addon cote Home Assistant) et :
 *   1) applique le fuseau horaire choisi (utilise par api.php pour l'export ICS) ;
 *   2) si un mot de passe de demarrage est fourni ET qu'aucun data/config.json
 *      n'existe encore, cree le compte "mode connecte" automatiquement, avec
 *      la meme structure que celle produite par setup.php (password_hash,
 *      share_token, ics_url vide a completer depuis les Parametres de l'app).
 *
 * N'ecrase jamais un data/config.json existant : ce bootstrap ne joue un role
 * qu'au tout premier demarrage. Les executions suivantes sont des no-op pour
 * la partie mot de passe.
 */

const DATA_DIR     = '/data';
const OPTIONS_FILE = DATA_DIR . '/options.json';
const CONFIG_FILE  = DATA_DIR . '/config.json';
const TZ_INI_FILE  = '/usr/local/etc/php/conf.d/bsa-timezone.ini';

function log_line(string $msg): void {
    fwrite(STDOUT, "[Brightspace Agenda] {$msg}\n");
}

if (!file_exists(OPTIONS_FILE)) {
    log_line('options.json introuvable, demarrage avec les valeurs par defaut.');
    exit(0);
}

$options = json_decode(file_get_contents(OPTIONS_FILE), true);
if (!is_array($options)) {
    log_line('AVERTISSEMENT : options.json illisible, demarrage avec les valeurs par defaut.');
    $options = [];
}

// ── Fuseau horaire ──────────────────────────────────────────────
$timezone = trim((string)($options['timezone'] ?? ''));
if ($timezone !== '') {
    if (in_array($timezone, timezone_identifiers_list(), true)) {
        file_put_contents(TZ_INI_FILE, "date.timezone = {$timezone}\n");
        log_line("Fuseau horaire applique : {$timezone}");
    } else {
        log_line("AVERTISSEMENT : fuseau horaire '{$timezone}' invalide, ignore (voir la liste PHP des identifiants de fuseaux).");
    }
}

// ── Provisionnement du compte au premier demarrage ─────────────
$dashboardPassword = (string)($options['dashboard_password'] ?? '');

if ($dashboardPassword === '') {
    log_line('Aucun mot de passe de demarrage fourni, configuration manuelle via /setup.php.');
    exit(0);
}

if (file_exists(CONFIG_FILE)) {
    log_line('data/config.json existe deja, mot de passe de demarrage ignore (deja configure).');
    exit(0);
}

if (strlen($dashboardPassword) < 6) {
    log_line('AVERTISSEMENT : mot de passe de demarrage trop court (min. 6 caracteres), configuration manuelle via /setup.php requise.');
    exit(0);
}

$config = [
    'password_hash' => password_hash($dashboardPassword, PASSWORD_DEFAULT),
    'share_token'   => bin2hex(random_bytes(16)),
    'ics_url'       => '',
];

$written = file_put_contents(CONFIG_FILE, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));

if ($written === false) {
    log_line('ERREUR : impossible d\'ecrire data/config.json (permissions sur /data ?). Configuration manuelle via /setup.php requise.');
    exit(1);
}

chmod(CONFIG_FILE, 0640);
log_line('Compte "mode connecte" cree automatiquement a partir du mot de passe de demarrage.');
log_line('Pense a renseigner l\'URL ICS Brightspace depuis les Parametres de l\'app (jamais stockee dans la config Home Assistant).');
