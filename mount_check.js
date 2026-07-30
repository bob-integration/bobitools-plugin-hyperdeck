// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
//
// Banc d'essai du montage de l'UI, sans navigateur.
//
//     node mount_check.js .        (depuis le dossier du plugin)
//
// À passer après TOUTE modification de page.js ou page.html.
//
// Motif : deux régressions d'affilée (fonction supprimée par une réécriture de bloc,
// identifiant absent du HTML) sont passées à travers `node --check`, qui ne vérifie que la
// syntaxe. Ici on EXÉCUTE réellement mount() contre un DOM minimal construit à partir des
// identifiants réellement présents dans page.html : toute référence à un élément absent ou
// à une fonction disparue lève, donc se voit.
//
// Ce n'est pas un rendu : ça ne dit rien de l'apparence. Ça dit seulement que le code se
// monte sans exploser — exactement la classe de panne qu'on vient de subir deux fois.
const fs = require("fs");
const path = process.argv[2];

const html = fs.readFileSync(path + "/page.html", "utf8");
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
const classes = new Set([...html.matchAll(/class="([^"]+)"/g)]
    .flatMap((m) => m[1].split(/\s+/)).filter(Boolean));

const calls = [];
const handlers = [];   // on rejouera les clics : c'est là que les régressions se voient
function makeEl(desc) {
    const el = {
        _desc: desc, dataset: {}, style: {}, hidden: false, checked: false,
        disabled: false, value: "", textContent: "", innerHTML: "", placeholder: "",
        maxLength: 0, id: "", className: "", title: "",
        classList: { toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false },
        addEventListener: (ev, fn) => { calls.push(desc + ":" + ev); handlers.push({ desc, ev, fn }); },
        querySelector: (s) => query(s),
        querySelectorAll: (s) => queryAll(s),
        appendChild: () => {}, closest: () => null, focus: () => {}, remove: () => {},
        getAttribute: () => null, setAttribute: () => {}, matches: () => false,
    };
    return el;
}
function query(sel) {
    if (sel.startsWith("#")) return ids.has(sel.slice(1)) ? makeEl(sel) : null;
    return makeEl(sel);
}
function queryAll(sel) {
    // Deux éléments : assez pour exercer les boucles sans exploser la sortie.
    return [makeEl(sel + "[0]"), makeEl(sel + "[1]")];
}

global.window = {};
global.document = {
    querySelector: query, querySelectorAll: queryAll,
    createElement: (t) => makeEl("<" + t + ">"),
};
global.BT = { esc: (s) => String(s == null ? "" : s), toast: () => {} };
global.window.BT = global.BT;
// L'outil confirme les gestes destructeurs (suppression, redémarrage) : on répond
// systématiquement « non » pour que le banc n'exécute jamais l'action elle-même.
global.window.confirm = () => false;
global.Blob = function () {};
global.URL = { createObjectURL: () => "blob:", revokeObjectURL: () => {} };
global.FileReader = function () { this.readAsText = () => {}; };

// Charge le module
const src = fs.readFileSync(path + "/page.js", "utf8");
eval(src);

const tool = global.window.BTTools && global.window.BTTools.hyperdeck;
if (!tool) { console.error("✗ window.BTTools.hyperdeck non défini"); process.exit(1); }

// ctx minimal : l'API renvoie des formes plausibles pour que le rendu s'exécute.
const SUMMARY = { connected: true, status: "record", model: "HyperDeck Studio 4K Pro",
                  timecode: "01:00:00:00", display_timecode: "01:00:00:00",
                  video_format: "1080p25", slot_id: "1", recording_time_h: "2 h 14",
                  remote_enabled: "false", file_format: "QuickTimeProResHQ" };
const STATE = {
    connected: true, error: null, model: "HyperDeck Studio 4K Pro",
    device: { model: "HyperDeck Studio 4K Pro", "slot count": "2", "unique id": "ABC" },
    transport: { status: "record", timecode: "01:00:00:00" },
    configuration: { "video input": "SDI", "file format": "QuickTimeProResHQ",
                     "record prefix": "Camera", "append timestamp": "false" },
    remote: { enabled: "false", override: "false" },
    xlr: [{ id: "1", type: "line" }],
    slots: { 1: { "slot id": "1", status: "mounted", "volume name": "SSD1",
                  "recording time": "8000", "video format": "1080p25" } },
    commands: ["play", "stop", "record"], extra: {}, events: [],
};
const shapes = {
    config: { refresh_interval: 20, live_timecode: true, default_port: 9993,
              sections: [{ key: "inputs", label: "Entrées" }, { key: "record", label: "Enregistrement" }] },
    decks: { decks: [{ id: "a1", name: "Deck 1", host: "10.0.0.1", port: 9993, enabled: true,
                       group: "Régie", notes: "", user: "", has_password: false,
                       summary: SUMMARY }], groups: ["Régie"] },
    overview: { rows: [{ id: "a1", name: "Deck 1", host: "10.0.0.1", group: "Régie",
                         enabled: true, summary: SUMMARY,
                         slots: [{ slot_id: "1", status: "mounted", recording_time_h: "2 h 14" }] }] },
    "decks/a1": {
        deck: { id: "a1", name: "Deck 1", host: "10.0.0.1", port: 9993, enabled: true, summary: SUMMARY },
        state: STATE,
        settings: [{ key: "video input", label: "Entrée vidéo", type: "enum",
                     choices: ["SDI", "HDMI"], section: "inputs", order: 10, value: "SDI" },
                   { key: "append timestamp", label: "Horodatage", type: "bool",
                     choices: [], section: "record", order: 40, value: "false" }],
        sections: [{ key: "inputs", label: "Entrées" }, { key: "record", label: "Enregistrement" }],
    },
    export: { decks: [], nas: [] },
    nas: { paths: [{ id: "n1", label: "Rushes", url: "smb://10.0.0.9/Rushes",
                     username: "user", has_password: true, notes: "" }] },
    "nas/matrix": {
        paths: [{ id: "n1", label: "Rushes", url: "smb://10.0.0.9/Rushes", username: "user", has_password: true }],
        decks: [{ id: "a1", name: "Deck 1", host: "10.0.0.1", connected: true,
                  nas_supported: true, mounted: true, mounted_url: "smb://10.0.0.9/Rushes" }],
        cells: { "n1|a1": { state: "mounted", title: "monté" } },
    },
};
const FALLBACK = { ok: true, response: { code: 200, text: "ok", ok: true, lines: [], params: {} },
                   state: STATE, results: [], clips: [], added: 0, updated: 0 };
const ctx = {
    t: (k) => k,
    toast: () => {},
    api: (p) => Promise.resolve(shapes[p] || shapes[p.split("?")[0]] || FALLBACK),
};

let failed = false;
process.on("unhandledRejection", (e) => { console.error("✗ promesse rejetée :", e.message); failed = true; });

try {
    tool.mount(makeEl("#root"), ctx);
    console.log("✓ mount() sans erreur ·", calls.length, "écouteurs posés");
} catch (e) {
    console.error("✗ mount() a levé :", e.message);
    console.error(e.stack.split("\n").slice(1, 4).join("\n"));
    failed = true;
}

// Rejoue TOUS les gestionnaires de clic et de changement. Une fonction supprimée par une
// réécriture, un identifiant absent du HTML : ça lève ici, pas au montage.
setTimeout(() => {
    let ko = 0;
    // Instantané AVANT de rejouer : un gestionnaire qui re-rend la vue pose de nouveaux
    // écouteurs, et parcourir un tableau qui s'allonge ne se termine jamais.
    const snapshot = handlers.slice();
    for (const h of snapshot) {
        if (h.ev !== "click" && h.ev !== "change" && h.ev !== "input") continue;
        try { h.fn({ target: makeEl("evt"), preventDefault: () => {}, stopPropagation: () => {} }); }
        catch (e) { console.error("✗ " + h.ev + " sur " + h.desc + " → " + e.message); ko++; failed = true; }
    }
    console.log((ko ? "✗ " : "✓ ") + handlers.length + " gestionnaires rejoués · " + ko + " en erreur");

    try { tool.unmount(); console.log("✓ unmount() sans erreur"); }
    catch (e) { console.error("✗ unmount() a levé :", e.message); failed = true; }
    process.exit(failed ? 1 : 0);
}, 300);
