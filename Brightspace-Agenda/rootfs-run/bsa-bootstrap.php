<?php
/**
 * bsa-bootstrap.php
 *
 * Exécuté une fois par démarrage du conteneur (voir run.sh), avant Apache.
 * Lit /data/options.json (config de l'addon côté Home Assistant) et :
 *   1) applique le fuseau horaire choisi (utilisé par api.php pour l'export ICS) ;
 *   2) si aucun data/config.json n'existe encore, crée le compte "mode connecté"
 *      automatiquement à partir du mot de passe saisi dans la configuration de
 *      l'addon (obligatoire, voir config.yaml), avec la même structure que
 *      celle produite par setup.php (password_hash, share_token, ics_url vide
 *      à compléter depuis les Paramètres de l'app).
 *
 * N'écrase jamais un data/config.json existant : ce bootstrap ne joue un rôle
 * qu'au tout premier démarrage. Les exécutions suivantes sont des no-op pour
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
    log_line('options.json introuvable, démarrage avec les valeurs par défaut.');
    exit(0);
}

$options = json_decode(file_get_contents(OPTIONS_FILE), true);
if (!is_array($options)) {
    log_line('AVERTISSEMENT : options.json illisible, démarrage avec les valeurs par défaut.');
    $options = [];
}

// ── Fuseau horaire ──────────────────────────────────────────────
$timezone = trim((string)($options['timezone'] ?? ''));
if ($timezone !== '') {
    if (in_array($timezone, timezone_identifiers_list(), true)) {
        file_put_contents(TZ_INI_FILE, "date.timezone = {$timezone}\n");
        log_line("Fuseau horaire appliqué : {$timezone}");
    } else {
        log_line("AVERTISSEMENT : fuseau horaire '{$timezone}' invalide, ignoré (voir la liste PHP des identifiants de fuseaux).");
    }
}

// ── Provisionnement du compte au premier démarrage ─────────────
// dashboard_password est obligatoire dans le schema (config.yaml) : le
// Supervisor ne laisse pas demarrer l'addon tant qu'il n'est pas renseigne.
// Le cas vide ci-dessous reste un filet de securite defensif (ex. options.json
// modifie a la main), pas un chemin normal.
$dashboardPassword = (string)($options['dashboard_password'] ?? '');

if ($dashboardPassword === '') {
    log_line('ERREUR : dashboard_password est vide alors qu\'il est obligatoire. Renseigne-le dans l\'onglet Configuration de l\'addon.');
    exit(1);
}

if (file_exists(CONFIG_FILE)) {
    log_line('data/config.json existe déjà, mot de passe de démarrage ignoré (déjà configuré).');
    exit(0);
}

if (strlen($dashboardPassword) < 6) {
    log_line('ERREUR : mot de passe trop court (min. 6 caractères). Corrige-le dans l\'onglet Configuration de l\'addon puis redémarre.');
    exit(1);
}

$config = [
    'password_hash' => password_hash($dashboardPassword, PASSWORD_DEFAULT),
    'share_token'   => bin2hex(random_bytes(16)),
    'ics_url'       => '',
];

$written = file_put_contents(CONFIG_FILE, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));

if ($written === false) {
    log_line('ERREUR : impossible d\'écrire data/config.json (permissions sur /data ?).');
    exit(1);
}

chmod(CONFIG_FILE, 0640);
log_line('Compte "mode connecté" créé automatiquement à partir du mot de passe de démarrage.');
log_line('Pense à renseigner l\'URL ICS Brightspace depuis les Paramètres de l\'app (jamais stockée dans la config Home Assistant).');
