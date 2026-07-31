// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// UI de « HyperDeck ». Front pur : toute la logique tourne dans le conteneur, atteinte via
// ctx.api(...).
//
// L'UI ne connaît AUCUN modèle de HyperDeck. Ce qu'elle affiche vient de ce que l'appareil
// a réellement déclaré : les réglages sont construits à partir de la réponse
// `configuration` de la machine, les slots à partir de `slot info`, les capacités à partir
// de la liste `commands`. Un firmware qui ajoute un réglage le fait apparaître ici sans
// qu'une ligne change.
//
// Rafraîchissement : le serveur sert un cache alimenté par les notifications de l'appareil,
// donc interroger toutes les deux secondes ne coûte rien au parc. Le suivi en direct
// s'interrompt de lui-même sur l'onglet Réglages dès qu'une valeur a été touchée — un
// rafraîchissement qui réécrit un champ en cours de saisie est une plaie.
window.BTTools = window.BTTools || {};
window.BTTools.hyperdeck = (function () {
    "use strict";
    let ctx = null, root = null;
    let decks = [], groups = [], conf = {};
    let selected = new Set();          // sélection multiple → actions groupées
    let selId = null;                  // machine affichée dans le détail
    let detail = null;                 // dernier /decks/<id> reçu
    let tab = "transport";
    let view = "fleet";
    let editing = null;                // id en cours d'édition dans le formulaire
    let settingsDirty = false;         // un réglage a été touché → on gèle le rafraîchissement
    let live = true;
    let timer = null;
    let media = { slot: "", clips: [], source: "disk", error: null };
    let bulkKeysFilled = false;        // la liste des réglages groupables n'est remplie qu'une fois
    let nasPaths = [];                 // bibliothèque de chemins réseau, tenue par l'outil
    let nasMatrix = { paths: [], decks: [], cells: {} };
    let nasTargets = new Set();        // machines cochées dans la vue Volumes réseau
    let nasEditing = null;
    // Horloge : lue à part (API d'administration HTTP), donc jamais servie par le cache
    // de notifications. `null` = pas encore lue, à distinguer d'une lecture en échec.
    let clockOne = null;               // horloge de la machine du détail
    let clockRows = [];                // horloge de tout le parc (vue d'ensemble)

    // États d'un croisement (chemin, machine). Quatre faits DISTINCTS : un signet n'est
    // pas un montage, et un partage « sélectionné » dont le serveur ne répond pas n'est
    // pas monté non plus. Chaque état porte son libellé — jamais la couleur seule.
    const NAS_CELL = {
        mounted: { label: "monté", cls: "on" },
        selected: { label: "sélectionné", cls: "warn" },
        bookmark: { label: "signet", cls: "part" },
        absent: { label: "—", cls: "off" },
        offline: { label: "hors ligne", cls: "off" },
        unsupported: { label: "non géré", cls: "off" },
    };
    let consoleLog = [];               // [{cmd, resp}] — le plus récent en tête

    const esc = (s) => (window.BT && BT.esc ? BT.esc(s) : String(s == null ? "" : s));
    const tr = (key, fb) => { const v = ctx && ctx.t ? ctx.t(key) : null; return (v && v !== key) ? v : fb; };
    const $ = (sel) => root.querySelector(sel);
    const toast = (m, k) => ctx.toast(m, k);
    const deckOf = (id) => decks.find((d) => d.id === id);

    // Libellés d'état transport. La couleur ne porte JAMAIS l'information seule (règle
    // « Statut > Couleur ») : chaque pastille est doublée de son libellé.
    const STATUS = {
        record: { label: "ENREGISTREMENT", cls: "rec" },
        play: { label: "Lecture", cls: "on" },
        forward: { label: "Avance rapide", cls: "on" },
        rewind: { label: "Retour rapide", cls: "on" },
        jog: { label: "Jog", cls: "on" },
        shuttle: { label: "Shuttle", cls: "on" },
        stopped: { label: "Arrêté", cls: "off" },
        preview: { label: "Preview (entrée)", cls: "warn" },
    };
    const SLOT_STATUS = {
        mounted: "monté", empty: "vide", mounting: "montage…", error: "erreur",
    };

    // ── Cycle de vie ─────────────────────────────────────────
    function mount(el, context) {
        ctx = context; root = el;
        applyI18n();
        $("#hd-add").addEventListener("click", () => showForm(null));
        $("#hd-refresh").addEventListener("click", () => { refresh(); loadDetail(true); });
        $("#hd-export").addEventListener("click", doExport);
        $("#hd-import-btn").addEventListener("click", () => { const f = $("#hd-import"); if (f && f.click) f.click(); });
        $("#hd-import").addEventListener("change", doImport);
        $("#hd-live").addEventListener("change", (e) => { live = !!e.target.checked; });
        $("#hd-form").addEventListener("submit", onFormSubmit);
        $("#hd-f-cancel").addEventListener("click", hideForm);
        $("#hd-selall").addEventListener("change", onSelectAll);
        $("#hd-bulk-clear").addEventListener("click", () => { selected.clear(); renderCards(); });
        $("#hd-bulk-rec").addEventListener("click", () => bulkTransport("record"));
        $("#hd-bulk-stop").addEventListener("click", () => bulkTransport("stop"));
        $("#hd-bulk-play").addEventListener("click", () => bulkTransport("play"));
        $("#hd-bulk-apply").addEventListener("click", bulkSettings);
        $("#hd-bulk-ntp-apply").addEventListener("click", bulkNtp);
        $("#hd-ov-refresh").addEventListener("click", loadOverview);
        $("#hd-nas-add").addEventListener("click", () => showNasForm(null));
        $("#hd-nas-form").addEventListener("submit", onNasFormSubmit);
        $("#hd-n-cancel").addEventListener("click", () => { $("#hd-nas-form").hidden = true; nasEditing = null; });
        $("#hd-nas-reread").addEventListener("click", rereadNas);
        $("#hd-nas-apply").addEventListener("click", applyNas);
        $("#hd-nas-all").addEventListener("click", () => { nasMatrix.decks.forEach((d) => nasTargets.add(d.id)); renderNasTargets(); });
        $("#hd-nas-none").addEventListener("click", () => { nasTargets.clear(); renderNasTargets(); });
        $("#hd-nas-list").addEventListener("click", onNasListClick);
        $("#hd-nas-targets").addEventListener("change", onNasTargetChange);
        $("#hd-nas-grid").addEventListener("click", onNasGridClick);
        root.querySelectorAll(".hd-view-tab").forEach((b) =>
            b.addEventListener("click", () => setView(b.dataset.view)));

        // Délégation : les cartes et le détail sont rendus en innerHTML, donc leurs
        // gestionnaires ne peuvent pas être posés une fois pour toutes sur les éléments.
        $("#hd-cards").addEventListener("click", onCardsClick);
        $("#hd-detail").addEventListener("click", onDetailClick);
        $("#hd-detail").addEventListener("change", onDetailChange);
        $("#hd-detail").addEventListener("submit", onDetailSubmit);

        loadConfig();
        // La bibliothèque de chemins est chargée dès le montage : l'onglet Réseau d'une
        // machine en a besoin, même si l'utilisateur n'ouvre jamais la vue Volumes réseau.
        loadNasPaths();
        refresh();
        timer = setInterval(tick, 2000);
    }

    function unmount() {
        if (timer) clearInterval(timer);
        timer = null;
    }

    function applyI18n() {
        root.querySelectorAll("[data-i18n]").forEach((n) => {
            const v = tr(n.dataset.i18n, null);
            if (v) n.textContent = v;
        });
    }

    function tick() {
        if (!live) return;
        if (view === "overview") { loadOverview(); return; }
        // La vue Volumes réseau se contente de la matrice : elle sert le cache du serveur,
        // donc aucune I/O vers les machines. Le formulaire ouvert la met en pause pour ne
        // pas réécrire une saisie en cours.
        if (view === "nas") { if (!nasEditing) loadNasMatrix(); return; }
        refresh();
        if (selId && !(tab === "settings" && settingsDirty)) loadDetail(false);
    }

    // ── Chargements ──────────────────────────────────────────
    async function loadConfig() {
        try {
            conf = await ctx.api("config");
            const m = $("#hd-meta");
            if (m) {
                m.textContent = tr("plugin.hyperdeck.connPermanent",
                    "connexions permanentes · relecture complète toutes les ")
                    + Math.round(conf.refresh_interval || 20) + " s";
            }
        } catch (e) { /* l'outil reste utilisable sans ces méta-données */ }
    }

    async function refresh() {
        try {
            const data = await ctx.api("decks");
            decks = data.decks || [];
            groups = data.groups || [];
            for (const id of Array.from(selected)) if (!deckOf(id)) selected.delete(id);
            if (selId && !deckOf(selId)) { selId = null; detail = null; }
            renderCards();
            renderGroups();
        } catch (e) {
            $("#hd-cards").innerHTML = '<p class="hd-empty">' + esc(e.message) + "</p>";
        }
    }

    async function loadDetail(force) {
        if (!selId) { renderDetail(); return; }
        try {
            detail = await ctx.api("decks/" + encodeURIComponent(selId));
        } catch (e) {
            detail = null;
            $("#hd-detail").innerHTML = '<p class="hd-empty">' + esc(e.message) + "</p>";
            return;
        }
        renderDetail(force);
    }

    async function loadOverview() {
        try {
            const data = await ctx.api("overview");
            // L'horloge vient d'une autre interface que l'état : deux appels, mais le
            // second tape un cache côté serveur (TTL), donc afficher la vue d'ensemble en
            // boucle n'interroge pas le parc en boucle.
            await loadClockAll(false);
            renderOverview(data.rows || []);
        } catch (e) {
            $("#hd-ov").innerHTML = '<p class="hd-empty">' + esc(e.message) + "</p>";
        }
    }

    function setView(v) {
        view = v || "fleet";
        root.querySelectorAll(".hd-view-tab").forEach((b) =>
            b.classList.toggle("on", b.dataset.view === view));
        const f = $("#hd-view-fleet"), o = $("#hd-view-overview"), n = $("#hd-view-nas");
        if (f) f.hidden = view !== "fleet";
        if (o) o.hidden = view !== "overview";
        if (n) n.hidden = view !== "nas";
        if (view === "overview") loadOverview();
        if (view === "nas") { loadNasPaths(); loadNasMatrix(); }
    }

    // ── Parc : cartes ────────────────────────────────────────
    function renderGroups() {
        const dl = $("#hd-groups");
        if (dl) dl.innerHTML = groups.map((g) => '<option value="' + esc(g) + '">').join("");
    }

    function statusBadge(s) {
        const st = STATUS[s.status] || null;
        if (!s.connected) {
            const why = s.error ? " · " + esc(s.error) : "";
            return '<span class="hd-badge off">' + esc(tr("plugin.hyperdeck.offline", "hors ligne")) + why + "</span>";
        }
        if (!st) return '<span class="hd-badge off">' + esc(s.status || "—") + "</span>";
        let extra = "";
        if (s.status === "play" && s.speed && String(s.speed) !== "100") extra = " " + esc(s.speed) + " %";
        return '<span class="hd-badge ' + st.cls + '">' + esc(st.label) + extra + "</span>";
    }

    function renderCards() {
        const host = $("#hd-cards");
        if (!decks.length) {
            host.innerHTML = '<p class="hd-empty">'
                + esc(tr("plugin.hyperdeck.none", "Aucune machine — utilisez « + Ajouter un HyperDeck »."))
                + "</p>";
        } else {
            host.innerHTML = decks.map(cardHtml).join("");
        }
        const n = decks.length;
        const cnt = $("#hd-count");
        if (cnt) cnt.textContent = n + (n > 1 ? " machines" : " machine");
        const sa = $("#hd-selall");
        if (sa) sa.checked = n > 0 && selected.size === n;
        renderBulkBar();
    }

    function cardHtml(d) {
        const s = d.summary || {};
        const dot = !d.enabled ? "" : (s.connected ? "ok" : (s.connected === false ? "err" : ""));
        const tc = s.display_timecode || s.timecode || "";
        const bits = [];
        if (s.model) bits.push(esc(s.model));
        if (s.video_format) bits.push(esc(s.video_format));
        if (s.recording_time_h) bits.push(esc(s.recording_time_h) + " restantes");
        return '<article class="hd-card' + (d.id === selId ? " sel" : "")
            + (d.enabled ? "" : " off") + '" data-id="' + esc(d.id) + '">'
            + '<div class="hd-card-h">'
            + '<input type="checkbox" data-pick="' + esc(d.id) + '"'
            + (selected.has(d.id) ? " checked" : "") + ">"
            + '<span class="hd-dot ' + dot + '"></span>'
            + "<strong>" + esc(d.name) + "</strong>"
            + statusBadge(s)
            + '<span class="hd-actions">'
            + '<button class="btn btn-sm" data-act="edit" data-id="' + esc(d.id) + '" type="button">✎</button>'
            + '<button class="btn btn-sm" data-act="del" data-id="' + esc(d.id) + '" type="button">✕</button>'
            + "</span></div>"
            + '<div class="hd-card-sub">'
            + '<span class="hd-ip">' + esc(d.host) + "</span>"
            + (tc ? '<span class="hd-tc">' + esc(tc) + "</span>" : "")
            + (bits.length ? "<span>" + bits.join(" · ") + "</span>" : "")
            + (d.enabled ? "" : '<span class="hd-tag">désactivée</span>')
            + (s.remote_enabled === "false" ? '<span class="hd-tag warn">remote coupé</span>' : "")
            + "</div></article>";
    }

    function onCardsClick(e) {
        const pick = e.target && e.target.dataset ? e.target.dataset.pick : null;
        if (pick) {
            if (e.target.checked) selected.add(pick); else selected.delete(pick);
            renderBulkBar();
            const sa = $("#hd-selall");
            if (sa) sa.checked = decks.length > 0 && selected.size === decks.length;
            return;
        }
        const btn = e.target.closest ? e.target.closest("button[data-act]") : null;
        if (btn) {
            if (btn.dataset.act === "edit") showForm(btn.dataset.id);
            else if (btn.dataset.act === "del") removeDeck(btn.dataset.id);
            return;
        }
        const card = e.target.closest ? e.target.closest(".hd-card") : null;
        if (card && card.dataset.id) select(card.dataset.id);
    }

    function select(id) {
        selId = id;
        detail = null;
        settingsDirty = false;
        media = { slot: "", clips: [], source: "disk", error: null };
        renderCards();
        $("#hd-detail").innerHTML = '<p class="hd-empty">'
            + esc(tr("plugin.hyperdeck.loading1", "Lecture de l'état…")) + "</p>";
        loadDetail(true);
    }

    function onSelectAll(e) {
        selected.clear();
        if (e.target.checked) decks.forEach((d) => selected.add(d.id));
        renderCards();
    }

    function renderBulkBar() {
        const bar = $("#hd-bulk");
        if (!bar) return;
        bar.hidden = selected.size === 0;
        const n = $("#hd-bulk-n");
        if (n) n.textContent = selected.size + (selected.size > 1 ? " machines" : " machine");
        const sel = $("#hd-bulk-key");
        if (sel && !bulkKeysFilled) {
            // Réglages proposés à l'application groupée : ceux qui ont un sens sur tout un
            // parc à la fois. Le reste se règle machine par machine.
            const keys = ["file format", "video input", "audio input", "record prefix",
                "timecode input", "record trigger", "default standard", "append timestamp"];
            sel.innerHTML = keys.map((k) => '<option value="' + esc(k) + '">' + esc(k) + "</option>").join("");
            bulkKeysFilled = true;
        }
    }

    // ── Formulaire d'ajout / édition ─────────────────────────
    function showForm(id) {
        editing = id || null;
        const d = id ? deckOf(id) : null;
        $("#hd-f-name").value = d ? d.name : "";
        $("#hd-f-host").value = d ? d.host : "";
        $("#hd-f-port").value = d ? (d.port || 9993) : 9993;
        $("#hd-f-group").value = d ? d.group : "";
        $("#hd-f-user").value = d ? d.user : "";
        $("#hd-f-password").value = "";
        $("#hd-f-notes").value = d ? d.notes : "";
        $("#hd-f-enabled").checked = d ? !!d.enabled : true;
        $("#hd-f-note").textContent = d && d.has_password
            ? tr("plugin.hyperdeck.pwKept", "Mot de passe enregistré — laissez vide pour le conserver.") : "";
        $("#hd-form").hidden = false;
    }

    function hideForm() {
        $("#hd-form").hidden = true;
        editing = null;
    }

    async function onFormSubmit(e) {
        if (e && e.preventDefault) e.preventDefault();
        const body = {
            name: $("#hd-f-name").value.trim(),
            host: $("#hd-f-host").value.trim(),
            port: parseInt($("#hd-f-port").value, 10) || 9993,
            group: $("#hd-f-group").value.trim(),
            user: $("#hd-f-user").value.trim(),
            password: $("#hd-f-password").value,
            notes: $("#hd-f-notes").value.trim(),
            enabled: !!$("#hd-f-enabled").checked,
        };
        if (!body.host) { toast(tr("plugin.hyperdeck.hostRequired", "Adresse IP requise"), "error"); return; }
        if (!body.name) body.name = body.host;
        try {
            if (editing) await ctx.api("decks/" + encodeURIComponent(editing), { method: "PUT", body });
            else await ctx.api("decks", { body });
            toast(tr("plugin.hyperdeck.saved", "Machine enregistrée"));
            hideForm();
            refresh();
        } catch (err) { toast(err.message, "error"); }
    }

    async function removeDeck(id) {
        const d = deckOf(id);
        if (!window.confirm(tr("plugin.hyperdeck.confirmDel", "Supprimer cette machine ?")
            + "\n" + ((d && d.name) || ""))) return;
        try {
            await ctx.api("decks/" + encodeURIComponent(id), { method: "DELETE" });
            if (selId === id) { selId = null; detail = null; }
            selected.delete(id);
            toast(tr("plugin.hyperdeck.deleted", "Supprimée"));
            refresh();
            renderDetail();
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Détail ───────────────────────────────────────────────
    function renderDetail(force) {
        const host = $("#hd-detail");
        if (!selId || !detail) {
            if (!selId) {
                host.innerHTML = '<p class="hd-empty">'
                    + esc(tr("plugin.hyperdeck.pick", "Sélectionnez une machine pour voir son détail.")) + "</p>";
            }
            return;
        }
        // L'onglet Réglages garde la main sur son DOM tant qu'une valeur est en cours
        // d'édition : le re-rendre effacerait la saisie.
        if (tab === "settings" && settingsDirty && !force) return;
        const d = detail.deck, st = detail.state || {}, s = d.summary || {};
        const dev = st.device || {};
        const tabs = [["transport", "Transport"], ["settings", "Réglages"], ["media", "Médias"],
            ["network", "Réseau"], ["clock", "Horloge"], ["events", "Événements"],
            ["console", "Console"]];
        host.innerHTML =
            '<div class="hd-detail-h">'
            + '<span class="hd-dot ' + (s.connected ? "ok" : "err") + '"></span>'
            + "<strong>" + esc(d.name) + "</strong>"
            + statusBadge(s)
            + '<span class="hd-detail-actions">'
            + '<button class="btn btn-sm" data-act="reconnect" type="button">Reconnecter</button>'
            + '<button class="btn btn-sm" data-act="identify" type="button">Identifier</button>'
            + "</span></div>"
            + '<div class="hd-ident">'
            + esc([dev.model || s.model || "modèle inconnu",
                dev["software version"] ? "logiciel " + dev["software version"] : "",
                dev["unique id"] ? "id " + dev["unique id"] : "",
                dev["protocol version"] ? "protocole " + dev["protocol version"] : "",
                d.host + ":" + d.port].filter(Boolean).join(" · "))
            + "</div>"
            + connBanner(st, s)
            + '<nav class="hd-tabs">'
            + tabs.map(([k, lab]) => '<button class="hd-tab' + (k === tab ? " on" : "")
                + '" data-tab="' + k + '" type="button">' + esc(lab) + "</button>").join("")
            + "</nav>"
            + '<div class="hd-tabbody">' + tabBody(st, s) + "</div>";
    }

    function connBanner(st, s) {
        if (!s.connected) {
            return '<div class="hd-banner err">'
                + esc(tr("plugin.hyperdeck.noConn", "Pas de connexion à la machine."))
                + (st.error ? " " + esc(st.error) : "")
                + " " + esc(tr("plugin.hyperdeck.retrying", "Nouvelle tentative automatique."))
                + "</div>";
        }
        // « remote » coupé = toute commande de transport sera refusée (111). C'est LA
        // cause d'incompréhension numéro un du protocole : on l'annonce avant que
        // l'utilisateur clique, avec le bouton qui la corrige.
        if (String((st.remote || {}).enabled) === "false") {
            return '<div class="hd-banner warn">'
                + esc(tr("plugin.hyperdeck.remoteOff",
                    "Le pilotage à distance est coupé sur la machine : les commandes de transport seront refusées."))
                + ' <button class="btn btn-sm" data-act="remote-on" type="button">Activer le pilotage</button>'
                + ' <button class="btn btn-sm" data-act="remote-override" type="button">Forcer pour cette session</button>'
                + "</div>";
        }
        return "";
    }

    function tabBody(st, s) {
        if (tab === "settings") return settingsHtml(st);
        if (tab === "media") return mediaHtml(st);
        if (tab === "network") return networkHtml(st);
        if (tab === "clock") return clockHtml();
        if (tab === "events") return eventsHtml(st);
        if (tab === "console") return consoleHtml(st);
        return transportHtml(st, s);
    }

    // ── Onglet Horloge (une machine) ─────────────────────────
    // L'heure et le NTP ne viennent PAS du protocole Ethernet, qui les ignore, mais de
    // l'API d'administration de la machine (HTTP). Deux faits distincts sont montrés
    // séparément, parce qu'ils ne disent pas la même chose : l'heure actuelle peut être
    // juste alors que la synchronisation est en échec — la machine a été mise à l'heure
    // un jour et n'a pas encore dérivé. C'est ce cas-là qu'on veut voir venir.
    function clockHtml() {
        const c = clockOne;
        if (c === null) return '<p class="hd-empty">Lecture de l\'horloge…</p>';
        if (!c.available) {
            return '<div class="hd-banner err">Horloge illisible : ' + esc(c.error || "—")
                + '</div><p class="hd-empty">Cette machine ne répond pas sur son interface '
                + "d'administration (HTTP). Vérifiez que l'accès web n'est pas désactivé "
                + "dans ses réglages réseau, ou que le firmware est assez récent.</p>";
        }
        const ntp = c.ntp || {};
        const cls = ntp.ok ? "ok" : (ntp.enabled === false ? "" : "err");
        return '<table class="hd-table"><tbody>'
            + row("Heure de la machine", fmtDeckTime(c))
            + row("Écart avec le serveur", driftHtml(c.drift))
            + row("Fuseau réglé sur la machine", tzLabel(c.tz_offset_min))
            + row("Synchronisation (NTP)", '<span class="hd-badge ' + cls + '">'
                + esc(ntp.state_label || "—") + "</span>")
            + row("Serveur de temps", '<span class="hd-mono">' + esc(ntp.server || "—") + "</span>")
            + "</tbody></table>"
            + '<div class="hd-clock-edit">'
            + '<label>Serveur de temps <input type="text" id="hd-ntp-server" class="hd-mono" '
            + 'value="' + esc(ntp.server || "") + '" placeholder="time.cloudflare.com"></label>'
            + '<label class="hd-inline"><input type="checkbox" id="hd-ntp-enabled"'
            + (ntp.enabled ? " checked" : "") + "> Synchroniser automatiquement</label>"
            + '<button class="btn btn-green" data-act="ntp-save" type="button">Appliquer</button>'
            + '<button class="btn btn-sm" data-act="clock-refresh" type="button">Relire</button>'
            + "</div>"
            + '<p class="hd-hint">La machine accepte n\'importe quelle adresse sans la '
            + "vérifier : c'est l'état de synchronisation, relu juste après, qui dit si "
            + "elle y arrive. Une horloge non synchronisée dérive, et c'est elle qui date "
            + "les fichiers enregistrés.</p>";
    }

    function row(label, value) {
        return "<tr><td>" + esc(label) + "</td><td>" + value + "</td></tr>";
    }

    async function loadClockOne() {
        if (!selId) return;
        clockOne = null;
        renderDetail(true);
        try {
            const d = await ctx.api("decks/" + selId + "/clock");
            clockOne = d.clock || { available: false, error: "réponse vide" };
        } catch (e) {
            clockOne = { available: false, error: e.message };
        }
        renderDetail(true);
    }

    async function saveNtp() {
        const el = $("#hd-ntp-server"), en = $("#hd-ntp-enabled");
        const server = el ? el.value.trim() : "";
        const enabled = en ? en.checked : false;
        if (enabled && !server) { ctx.toast("Indiquez l'adresse du serveur de temps", "warning"); return; }
        try {
            const d = await ctx.api("decks/" + selId + "/clock", { body: { server, enabled } });
            clockOne = d.clock || clockOne;
            const st = (d.ntp || {});
            // On rend compte de l'état RÉEL, pas d'un « enregistré » qui laisserait croire
            // que la synchronisation fonctionne : la machine accepte toute adresse.
            ctx.toast(st.ok ? "Serveur de temps appliqué — " + (st.state_label || "")
                : "Enregistré, mais la synchronisation n'aboutit pas (" + (st.state_label || "?") + ")",
                st.ok ? "info" : "warning");
            renderDetail(true);
        } catch (e) { ctx.toast(e.message, "error"); }
    }

    // Cellule « Horloge » de la vue d'ensemble : l'heure de la machine, et l'état de
    // synchronisation en dessous. Une heure juste avec un NTP en échec reste signalée —
    // c'est l'avertissement utile, celui qui précède la dérive.
    function clockCell(deckId) {
        const r = clockRows.find((x) => x.id === deckId);
        if (!r || !r.clock) return '<span class="hd-empty-cell">—</span>';
        const c = r.clock;
        if (!c.available) {
            return '<span class="hd-badge" title="' + esc(c.error || "") + '">indisponible</span>';
        }
        const ntp = c.ntp || {};
        const cls = ntp.ok ? "ok" : (ntp.enabled === false ? "" : "err");
        return fmtDeckTime(c) + "<br>" + driftBadge(c.drift)
            + ' <span class="hd-badge ' + cls + '" title="Serveur : '
            + esc(ntp.server || "—") + '">' + esc(ntp.state_label || "?") + "</span>";
    }

    function driftBadge(drift) {
        if (drift === null || drift === undefined) return "";
        const a = Math.abs(drift);
        if (a <= 2) return "";                 // écart normal : on n'encombre pas la ligne
        const cls = a <= 60 ? "warn" : "err";
        const txt = a < 90 ? Math.round(drift) + " s"
            : a < 5400 ? (drift / 60).toFixed(1) + " min" : (drift / 3600).toFixed(1) + " h";
        return '<span class="hd-badge ' + cls + '" title="Écart avec l\'heure du serveur">'
            + (drift > 0 ? "+" : "") + esc(txt) + "</span>";
    }

    async function loadClockAll(force) {
        try {
            const d = await ctx.api("clock" + (force ? "?refresh=1" : ""));
            clockRows = d.rows || [];
        } catch (e) { clockRows = []; }
    }

    // L'heure telle que LA MACHINE la voit : epoch absolu ramené dans le fuseau qu'elle
    // déclare. C'est cette heure-là qui datera ses fichiers, pas celle du navigateur.
    function fmtDeckTime(c) {
        if (!c.epoch) return "—";
        const d = new Date((c.epoch - (c.tz_offset_min || 0) * 60) * 1000);
        const p = (n) => String(n).padStart(2, "0");
        return '<span class="hd-mono">' + d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1)
            + "-" + p(d.getUTCDate()) + " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes())
            + ":" + p(d.getUTCSeconds()) + "</span>";
    }

    function tzLabel(min) {
        if (min === null || min === undefined) return "—";
        if (min === 0) return "UTC";
        // Convention JavaScript : minutes À L'OUEST de UTC. −120 ⇒ UTC+2.
        const east = -min, h = Math.trunc(east / 60), m = Math.abs(east % 60);
        return "UTC" + (east >= 0 ? "+" : "−") + Math.abs(h) + (m ? ":" + String(m).padStart(2, "0") : "");
    }

    function driftHtml(drift) {
        if (drift === null || drift === undefined) return "—";
        const a = Math.abs(drift);
        const cls = a <= 2 ? "ok" : a <= 60 ? "warn" : "err";
        const txt = a < 90 ? Math.round(drift) + " s"
            : a < 5400 ? (drift / 60).toFixed(1) + " min"
                : (drift / 3600).toFixed(1) + " h";
        return '<span class="hd-badge ' + cls + '">' + (drift > 0 ? "+" : "") + esc(txt) + "</span>"
            + (a > 2 ? ' <span class="hd-hint-inline">la machine est '
                + (drift > 0 ? "en avance" : "en retard") + " sur le serveur</span>" : "");
    }

    // ── Onglet Réseau (une machine) ──────────────────────────
    // Montre CE QUE LA MACHINE RAPPORTE, en séparant les trois notions que le protocole
    // distingue et qu'on confond facilement : signets mémorisés, partage sélectionné, et
    // slot réseau réellement monté. Les lignes non documentées du protocole sont affichées
    // brutes plutôt que réinterprétées.
    function networkHtml(st) {
        const nas = st.nas || {};
        if (nas.supported === false) {
            return '<p class="hd-empty">Cette machine ne répond pas aux commandes réseau '
                + "(firmware plus ancien)." + (nas.error ? " " + esc(nas.error) : "") + "</p>";
        }
        const slot = nas.network_slot || {};
        const mounted = slot.status === "mounted";
        let html = '<div class="hd-inline">'
            + "<label>Chemin <select id=\"hd-net-pick\">"
            + nasPaths.map((p) => '<option value="' + esc(p.id) + '">' + esc(p.label)
                + " — " + esc(p.url) + "</option>").join("")
            + "</select></label>"
            + '<button class="btn btn-sm" data-act="net-mount" type="button">Enregistrer et monter</button>'
            + '<button class="btn btn-sm" data-act="net-add" type="button">Signet seulement</button>'
            + '<button class="btn btn-sm" data-act="net-deselect" type="button">Démonter</button>'
            + '<button class="btn btn-sm" data-act="net-refresh" type="button">↻ Relire</button>'
            + '<button class="btn btn-sm" data-act="net-discover" type="button">Chercher les serveurs (mDNS)</button>'
            + "</div>";
        if (!nasPaths.length) {
            html += '<p class="hd-empty">Aucun chemin dans la bibliothèque — créez-en un dans '
                + "l'onglet « Volumes réseau ».</p>";
        }

        html += "<h4>Volume réseau monté</h4>";
        if (mounted) {
            const rows = [["Adresse", slot.url || "—"], ["Volume", slot["volume name"] || "—"],
                ["État", slot.status], ["Restant", fmtDuration(slot["recording time"])],
                ["Format", slot["video format"] || "—"]];
            html += '<dl class="hd-kv">' + rows.map(([k, v]) =>
                "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>").join("") + "</dl>";
        } else {
            html += '<p class="hd-empty">Aucun volume réseau monté sur cette machine'
                + (slot.status ? " (slot réseau : " + esc(slot.status) + ")" : "") + ".</p>";
        }

        html += "<h4>Partage sélectionné</h4>";
        const sel = nas.selected || [];
        if (!sel.length) {
            html += '<p class="hd-empty">Aucun.</p>';
        } else {
            html += nasEntriesHtml(sel);
            if (!mounted) {
                // Le cas piégeux : sélectionné mais pas monté. Le dire explicitement plutôt
                // que laisser croire à un volume disponible.
                html += '<div class="hd-banner warn">Un partage est sélectionné mais aucun '
                    + "volume réseau n'est monté : le serveur n'a pas (encore) répondu.</div>";
            }
        }

        html += "<h4>Signets enregistrés dans la machine</h4>";
        const bm = nas.bookmarks || [];
        html += bm.length ? nasEntriesHtml(bm) : '<p class="hd-empty">Aucun signet.</p>';

        const disc = nas.discovered || [];
        if (disc.length) {
            html += "<h4>Serveurs découverts (mDNS)</h4>"
                + '<pre class="hd-cons-out">' + esc(disc.join("\n")) + "</pre>";
        }
        if (nas.error) html += '<p class="hd-empty">' + esc(nas.error) + "</p>";
        if (nas.read_at) html += '<p class="hd-meta">relu à ' + esc(hhmmss(nas.read_at)) + "</p>";
        return html;
    }

    function nasEntriesHtml(entries) {
        // `raw` est toujours affiché : le protocole ne documente pas le format de ces
        // réponses, donc la ligne d'origine reste la seule référence sûre.
        return '<table class="hd-table"><thead><tr><th>Adresse</th><th>Ligne renvoyée</th>'
            + "</tr></thead><tbody>"
            + entries.map((e) => '<tr><td class="hd-mono">' + esc(e.url || "—")
                + '</td><td class="hd-mono">' + esc([e.raw].concat(e.details || []).join(" · "))
                + "</td></tr>").join("")
            + "</tbody></table>";
    }

    // ── Onglet Transport ─────────────────────────────────────
    function transportHtml(st, s) {
        const tr_ = st.transport || {};
        const slots = st.slots || {};
        const rows = [
            ["État", (STATUS[s.status] || {}).label || s.status || "—"],
            ["Timecode", tr_.timecode || "—"],
            ["Timecode affiché", tr_["display timecode"] || "—"],
            ["Format de lecture", tr_["video format"] || "—"],
            ["Format en entrée", tr_["input video format"] || "—"],
            ["Clip", tr_["clip id"] || "—"],
            ["Boucle", yn(tr_.loop)],
            ["Clip seul", yn(tr_["single clip"])],
            ["Référence verrouillée", yn(tr_["reference locked"])],
            ["Pilotage à distance", yn((st.remote || {}).enabled)
                + (String((st.remote || {}).override) === "true" ? " (forcé pour la session)" : "")],
        ];
        const slotIds = Object.keys(slots).sort();
        return '<div class="hd-transport">'
            + '<div class="hd-tbtns">'
            + '<button class="btn btn-red hd-big" data-act="rec" type="button">⏺ REC</button>'
            + '<button class="btn hd-big" data-act="stop" type="button">⏹ STOP</button>'
            + '<button class="btn btn-green hd-big" data-act="play" type="button">▶ PLAY</button>'
            + '<button class="btn hd-big" data-act="preview" type="button">Preview / Sortie</button>'
            + "</div>"
            + '<div class="hd-inline">'
            + '<label>Nom du clip <input type="text" id="hd-recname" placeholder="(préfixe des réglages si vide)"></label>'
            + '<label>Vitesse <input type="number" id="hd-speed" value="100" min="-5000" max="5000" step="25"> %</label>'
            + '<button class="btn btn-sm" data-act="play-speed" type="button">Lire à cette vitesse</button>'
            + "</div>"
            + '<div class="hd-inline">'
            + '<button class="btn btn-sm" data-act="goto-start" type="button">⏮ Début</button>'
            + '<button class="btn btn-sm" data-act="goto-end" type="button">⏭ Fin</button>'
            + '<label>Aller au TC <input type="text" id="hd-goto-tc" placeholder="HH:MM:SS:FF"></label>'
            + '<button class="btn btn-sm" data-act="goto-tc" type="button">Aller</button>'
            + "</div>"
            + '<dl class="hd-kv">' + rows.map(([k, v]) =>
                "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>").join("") + "</dl>"
            + "<h4>Slots</h4>"
            + (slotIds.length ? '<table class="hd-table"><thead><tr>'
                + "<th>Slot</th><th>État</th><th>Volume</th><th>Restant</th><th>Format</th><th></th>"
                + "</tr></thead><tbody>"
                + slotIds.map((id) => slotRow(id, slots[id], s)).join("")
                + "</tbody></table>"
                : '<p class="hd-empty">Aucun slot rapporté.</p>')
            + '<div class="hd-danger">'
            + '<button class="btn btn-sm" data-act="reboot" type="button">Redémarrer la machine</button>'
            + "</div>"
            + "</div>";
    }

    function slotRow(id, slot, s) {
        const active = String(s.slot_id || "") === String(id);
        const status = SLOT_STATUS[slot.status] || slot.status || "—";
        const blocked = String(slot.blocked) === "true";
        return "<tr" + (active ? ' class="on"' : "") + ">"
            + "<td>" + esc(id) + (active ? " ●" : "") + "</td>"
            + "<td>" + esc(status) + (blocked ? " (bloqué)" : "") + "</td>"
            + "<td>" + esc(slot["volume name"] || slot["device name"] || "—") + "</td>"
            + "<td>" + esc(fmtDuration(slot["recording time"])) + "</td>"
            + "<td>" + esc(slot["video format"] || "—") + "</td>"
            + '<td><button class="btn btn-sm" data-act="slot" data-slot="' + esc(id) + '" type="button">'
            + (active ? "actif" : "activer") + "</button></td></tr>";
    }

    // ── Onglet Réglages ──────────────────────────────────────
    function settingsHtml(st) {
        const params = detail.settings || [];
        if (!params.length) {
            return '<p class="hd-empty">Aucun réglage lu pour l\'instant '
                + "(la machine n'a pas encore répondu, ou la connexion est coupée).</p>";
        }
        const sections = detail.sections || [];
        let html = '<form class="hd-settings" id="hd-settings">';
        for (const sec of sections) {
            const items = params.filter((p) => p.section === sec.key);
            if (!items.length) continue;
            html += "<h4>" + esc(sec.label) + "</h4><div class=\"hd-set-grid\">";
            for (const p of items) html += settingRow(p);
            html += "</div>";
        }
        const xlr = st.xlr || [];
        if (xlr.length) {
            html += "<h4>Entrées XLR</h4><div class=\"hd-set-grid\">";
            for (const x of xlr) {
                html += "<label>Entrée XLR " + esc(x.id) + "</label><span>"
                    + '<select data-xlr="' + esc(x.id) + '">'
                    + ["line", "mic"].map((t) => '<option value="' + t + '"'
                        + (t === x.type ? " selected" : "") + ">" + t + "</option>").join("")
                    + "</select></span>";
            }
            html += "</div>";
        }
        html += '<div class="hd-form-actions">'
            + '<button class="btn btn-green" type="submit">Appliquer les modifications</button>'
            + '<button class="btn" data-act="settings-reload" type="button">Relire la machine</button>'
            + '<span class="hd-meta" id="hd-set-state">' + (settingsDirty ? "modifications non appliquées" : "") + "</span>"
            + "</div></form>"
            + '<div id="hd-set-report"></div>';
        return html;
    }

    function settingRow(p) {
        const id = "hd-s-" + p.key.replace(/[^a-z0-9]+/gi, "-");
        let control;
        if (p.type === "bool") {
            control = '<input type="checkbox" class="ios-toggle" id="' + id + '" data-skey="'
                + esc(p.key) + '" data-stype="bool"' + (String(p.value) === "true" ? " checked" : "") + ">";
        } else if (p.type === "enum" && (p.choices || []).length) {
            control = '<select id="' + id + '" data-skey="' + esc(p.key) + '" data-stype="enum">'
                + p.choices.map((c) => '<option value="' + esc(c) + '"'
                    + (c === p.value ? " selected" : "") + ">" + esc(c) + "</option>").join("")
                + "</select>";
        } else if (p.type === "number") {
            control = '<input type="number" id="' + id + '" data-skey="' + esc(p.key)
                + '" data-stype="number" value="' + esc(p.value) + '">';
        } else {
            control = '<input type="text" id="' + id + '" data-skey="' + esc(p.key)
                + '" data-stype="text" value="' + esc(p.value) + '"'
                + (p.placeholder ? ' placeholder="' + esc(p.placeholder) + '"' : "") + ">";
        }
        return '<label for="' + id + '">' + esc(p.label)
            + (p.unknown ? ' <span class="hd-tag">clé brute</span>' : "")
            + (p.note ? '<span class="hd-note">' + esc(p.note) + "</span>" : "")
            + "</label><span>" + control + "</span>";
    }

    // ── Onglet Médias ────────────────────────────────────────
    function mediaHtml(st) {
        const slots = Object.keys(st.slots || {}).sort();
        let html = '<div class="hd-inline">'
            + "<label>Disque <select id=\"hd-media-slot\">"
            + '<option value="">slot actif</option>'
            + slots.map((s) => '<option value="' + esc(s) + '"'
                + (media.slot === s ? " selected" : "") + ">slot " + esc(s) + "</option>").join("")
            + "</select></label>"
            + '<button class="btn btn-sm" data-act="media-disk" type="button">Lister le disque</button>'
            + '<button class="btn btn-sm" data-act="media-timeline" type="button">Lister la timeline</button>'
            + "</div>";
        if (media.error) html += '<p class="hd-empty">' + esc(media.error) + "</p>";
        if (!media.clips.length) {
            html += '<p class="hd-empty">'
                + (media.error ? "" : "Aucune liste chargée — cliquez sur « Lister le disque ».") + "</p>";
        } else {
            html += '<p class="hd-meta">' + media.clips.length + " élément(s) · "
                + (media.source === "disk" ? "disque" : "timeline") + "</p>"
                + '<table class="hd-table"><thead><tr><th>#</th><th>Clip</th></tr></thead><tbody>'
                + media.clips.map((c) => "<tr><td>" + esc(c.index) + '</td><td class="hd-mono">'
                    + esc(c.text) + "</td></tr>").join("")
                + "</tbody></table>";
        }
        return html;
    }

    // ── Onglet Événements ────────────────────────────────────
    function eventsHtml(st) {
        const evs = st.events || [];
        const extra = st.extra || {};
        const keys = Object.keys(extra).sort();
        let html = "";
        if (keys.length) {
            // Messages asynchrones que l'outil ne sait pas nommer (images perdues, position
            // timeline selon le firmware…). Affichés BRUTS plutôt que jetés : l'appareil
            // les envoie, ils veulent dire quelque chose.
            html += "<h4>Messages non interprétés</h4><table class=\"hd-table\"><thead><tr>"
                + "<th>Code</th><th>Libellé</th><th>Paramètres</th></tr></thead><tbody>"
                + keys.map((k) => "<tr><td>" + esc(k) + "</td><td>" + esc(extra[k].text)
                    + '</td><td class="hd-mono">' + esc(kvText(extra[k].params)) + "</td></tr>").join("")
                + "</tbody></table>";
        }
        html += "<h4>Journal des notifications</h4>";
        if (!evs.length) return html + '<p class="hd-empty">Rien reçu pour l\'instant.</p>';
        return html + '<table class="hd-table"><thead><tr><th>Heure</th><th>Code</th>'
            + "<th>Libellé</th><th>Paramètres</th></tr></thead><tbody>"
            + evs.map((e) => "<tr><td>" + esc(hhmmss(e.at)) + "</td><td>" + esc(e.code)
                + "</td><td>" + esc(e.text) + '</td><td class="hd-mono">'
                + esc(kvText(e.params)) + "</td></tr>").join("")
            + "</tbody></table>";
    }

    // ── Onglet Console ───────────────────────────────────────
    function consoleHtml(st) {
        const cmds = st.commands || [];
        return '<p class="hd-meta">Commande envoyée telle quelle à la machine. '
            + (cmds.length ? cmds.length + " commandes déclarées par ce firmware." : "")
            + "</p>"
            + '<form class="hd-inline" id="hd-console-form">'
            + '<input type="text" id="hd-console-cmd" list="hd-console-list" placeholder="transport info" style="flex:1">'
            + "<datalist id=\"hd-console-list\">"
            + cmds.map((c) => '<option value="' + esc(c) + '">').join("")
            + "</datalist>"
            + '<button class="btn" type="submit">Envoyer</button>'
            + "</form>"
            + consoleLog.map((l) => '<div class="hd-cons">'
                + '<div class="hd-cons-cmd">→ ' + esc(l.cmd) + "</div>"
                + '<pre class="hd-cons-out' + (l.resp.ok ? "" : " err") + '">'
                + esc(l.resp.code + " " + l.resp.text
                    + (l.resp.lines.length ? "\n" + l.resp.lines.join("\n") : "")) + "</pre></div>").join("");
    }

    // ── Interactions du détail ───────────────────────────────
    function onDetailClick(e) {
        const t = e.target.closest ? e.target.closest("[data-tab],[data-act]") : null;
        if (!t) return;
        if (t.dataset.tab) {
            tab = t.dataset.tab;
            settingsDirty = false;
            renderDetail(true);
            // L'horloge n'arrive pas avec l'état : il faut aller la chercher, et le faire
            // seulement quand on ouvre l'onglet — inutile d'interroger l'API
            // d'administration de chaque machine qu'on ne fait que survoler.
            if (tab === "clock") loadClockOne();
            return;
        }
        const act = t.dataset.act;
        if (act === "clock-refresh") return loadClockOne();
        if (act === "ntp-save") return saveNtp();
        if (act === "reconnect") return doReconnect();
        if (act === "identify") return transport("identify", { enable: true });
        if (act === "remote-on") return transport("remote", { enable: true });
        if (act === "remote-override") return transport("remote", { override: true });
        if (act === "rec") {
            const el = $("#hd-recname");
            return transport("record", { name: el ? el.value.trim() : "" });
        }
        if (act === "stop") return transport("stop", {});
        if (act === "play") return transport("play", {});
        if (act === "play-speed") {
            const el = $("#hd-speed");
            return transport("play", { speed: el ? parseInt(el.value, 10) || 100 : 100 });
        }
        if (act === "preview") {
            const st = (detail && detail.state) || {};
            const isPreview = ((st.transport || {}).status === "preview");
            return transport("preview", { enable: !isPreview });
        }
        if (act === "goto-start") return transport("goto", { kind: "timeline", target: "start" });
        if (act === "goto-end") return transport("goto", { kind: "timeline", target: "end" });
        if (act === "goto-tc") {
            const el = $("#hd-goto-tc");
            const v = el ? el.value.trim() : "";
            if (!v) return toast("Timecode requis", "error");
            return transport("goto", { kind: "timecode", target: v });
        }
        if (act === "slot") return transport("slot", { slot_id: parseInt(t.dataset.slot, 10) || 1 });
        if (act === "reboot") {
            if (!window.confirm("Redémarrer la machine ? Toute lecture ou tout enregistrement en cours sera interrompu.")) return;
            return transport("reboot", {});
        }
        if (act === "settings-reload") { settingsDirty = false; return loadDetail(true); }
        if (act === "media-disk") return loadMedia("disk");
        if (act === "media-timeline") return loadMedia("timeline");
        if (act === "net-mount") return deckNasAction("add_select");
        if (act === "net-add") return deckNasAction("add");
        if (act === "net-deselect") return deckNasAction("deselect");
        if (act === "net-refresh") return loadDeckNas(false);
        if (act === "net-discover") return loadDeckNas(true);
    }

    async function deckNasAction(action) {
        const el = $("#hd-net-pick");
        const nasId = el ? el.value : "";
        if (action !== "deselect" && !nasId) {
            toast("Aucun chemin choisi — créez-en un dans « Volumes réseau »", "error");
            return;
        }
        try {
            const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/nas",
                { body: { action, nas_id: nasId } });
            const steps = data.results || {};
            const ko = Object.keys(steps).filter((k) => !steps[k].ok);
            // Chaque étape (signet, montage) porte sa propre réponse : dire « OK » alors que
            // le signet est passé et le montage refusé serait faux.
            const txt = Object.keys(steps).map((k) => k + " : " + steps[k].code + " " + steps[k].text).join(" · ");
            toast(txt || "fait", ko.length ? "warning" : "info");
            if (detail && data.nas) detail.state.nas = data.nas;
            renderDetail(true);
        } catch (e) { toast(e.message, "error"); }
    }

    async function loadDeckNas(discover) {
        try {
            const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/nas"
                + (discover ? "?discover=1" : "?refresh=1"));
            if (detail && data.nas) detail.state.nas = data.nas;
            renderDetail(true);
        } catch (e) { toast(e.message, "error"); }
    }

    function onDetailChange(e) {
        const el = e.target;
        if (!el || !el.dataset) return;
        if (el.dataset.skey || el.dataset.xlr) {
            settingsDirty = true;
            const s = $("#hd-set-state");
            if (s) s.textContent = "modifications non appliquées";
            return;
        }
        if (el.id === "hd-media-slot") media.slot = el.value;
    }

    function onDetailSubmit(e) {
        if (e && e.preventDefault) e.preventDefault();
        const id = e.target && e.target.id;
        if (id === "hd-settings") return applySettings();
        if (id === "hd-console-form") return sendConsole();
    }

    // ── Actions ──────────────────────────────────────────────
    async function transport(action, body) {
        if (!selId) return;
        try {
            const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/transport",
                { body: Object.assign({ action }, body || {}) });
            const r = data.response || {};
            // Un refus de la machine (111 remote disabled, 105 no disk…) est une réponse
            // valide : on la montre telle quelle plutôt que de prétendre à une panne.
            if (r.ok) toast(r.code + " " + r.text);
            else toast(r.code + " " + r.text, "warning");
            if (data.state) { detail = Object.assign({}, detail, { state: data.state }); }
            renderDetail(true);
            refresh();
        } catch (e) { toast(e.message, "error"); }
    }

    async function doReconnect() {
        try {
            await ctx.api("decks/" + encodeURIComponent(selId) + "/reconnect", { body: {} });
            toast(tr("plugin.hyperdeck.reconnecting", "Reconnexion en cours…"));
        } catch (e) { toast(e.message, "error"); }
    }

    async function applySettings() {
        const values = {};
        root.querySelectorAll("#hd-settings [data-skey]").forEach((el) => {
            const key = el.dataset.skey;
            const cur = (detail.settings || []).find((p) => p.key === key);
            let v;
            if (el.dataset.stype === "bool") v = !!el.checked;
            else v = el.value;
            const before = cur ? (cur.type === "bool" ? String(cur.value) === "true" : String(cur.value)) : null;
            const after = (el.dataset.stype === "bool") ? v : String(v);
            // On n'envoie QUE ce qui a changé : réécrire un format de fichier identique
            // peut faire redémarrer la machine pour rien.
            if (before === null || before !== after) values[key] = v;
        });
        const xlrs = [];
        root.querySelectorAll("#hd-settings [data-xlr]").forEach((el) => {
            const st = (detail.state || {}).xlr || [];
            const cur = st.find((x) => String(x.id) === String(el.dataset.xlr));
            if (!cur || cur.type !== el.value) xlrs.push({ id: el.dataset.xlr, type: el.value });
        });
        if (!Object.keys(values).length && !xlrs.length) {
            toast(tr("plugin.hyperdeck.noChange", "Aucune modification"), "warning");
            return;
        }
        try {
            let results = [];
            if (Object.keys(values).length) {
                const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/settings", { body: { values } });
                results = data.results || [];
            }
            for (const x of xlrs) {
                // `xlr input id` et `xlr type` vont par paire : le protocole exige que les
                // deux soient dans la MÊME commande, sinon la machine ne sait pas de quelle
                // entrée on parle.
                const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/command",
                    { body: { command: "configuration: xlr input id: " + x.id + " xlr type: " + x.type } });
                results.push(Object.assign({ key: "xlr " + x.id, value: x.type }, data.response || {}));
            }
            settingsDirty = false;
            await loadDetail(true);
            showSettingsReport(results);
        } catch (e) { toast(e.message, "error"); }
    }

    function showSettingsReport(results) {
        const host = $("#hd-set-report");
        const ko = results.filter((r) => !r.ok);
        if (!host) {
            toast(ko.length ? ko.length + " réglage(s) refusé(s)" : "Réglages appliqués",
                ko.length ? "warning" : "info");
            return;
        }
        host.innerHTML = '<table class="hd-table"><thead><tr><th>Réglage</th><th>Valeur</th>'
            + "<th>Réponse</th></tr></thead><tbody>"
            + results.map((r) => '<tr class="' + (r.ok ? "" : "ko") + '"><td>' + esc(r.key)
                + "</td><td>" + esc(r.value) + "</td><td>" + esc(r.code + " " + r.text)
                + "</td></tr>").join("")
            + "</tbody></table>";
        toast(ko.length ? ko.length + " réglage(s) refusé(s) par la machine" : "Réglages appliqués",
            ko.length ? "warning" : "info");
    }

    async function loadMedia(kind) {
        const slotEl = $("#hd-media-slot");
        if (slotEl) media.slot = slotEl.value;
        media.error = null;
        try {
            const path = kind === "timeline"
                ? "decks/" + encodeURIComponent(selId) + "/timeline"
                : "decks/" + encodeURIComponent(selId) + "/clips"
                    + (media.slot ? "?slot=" + encodeURIComponent(media.slot) : "");
            const data = await ctx.api(path);
            media.clips = data.clips || [];
            media.source = kind;
            const r = data.response || {};
            if (!r.ok) media.error = r.code + " " + r.text;
        } catch (e) {
            media.clips = [];
            media.error = e.message;
        }
        renderDetail(true);
    }

    async function sendConsole() {
        const el = $("#hd-console-cmd");
        const cmd = el ? el.value.trim() : "";
        if (!cmd) return;
        try {
            const data = await ctx.api("decks/" + encodeURIComponent(selId) + "/command", { body: { command: cmd } });
            consoleLog.unshift({ cmd, resp: data.response || { code: 0, text: "", lines: [], ok: false } });
            consoleLog = consoleLog.slice(0, 12);
            renderDetail(true);
        } catch (e) { toast(e.message, "error"); }
    }

    // ── Actions groupées ─────────────────────────────────────
    async function bulkTransport(action) {
        if (!selected.size) return;
        const body = { action, ids: Array.from(selected) };
        if (action === "record") {
            const el = $("#hd-bulk-name");
            const n = el ? el.value.trim() : "";
            if (n) body.name = n;
        }
        await runBulk(body);
    }

    async function bulkSettings() {
        if (!selected.size) return;
        const key = $("#hd-bulk-key").value;
        const value = $("#hd-bulk-value").value.trim();
        if (!key || !value) { toast("Réglage et valeur requis", "error"); return; }
        if (!window.confirm("Appliquer « " + key + " : " + value + " » sur "
            + selected.size + " machine(s) ?")) return;
        await runBulk({ action: "settings", ids: Array.from(selected), values: { [key]: value } });
    }

    // Poser le même serveur de temps sur tout un parc. C'est le geste qui répare une
    // dérive : un enregistreur mal synchronisé date mal ses fichiers, et rien dans les
    // écrans de transport ne le laisse voir.
    async function bulkNtp() {
        if (!selected.size) return;
        const server = $("#hd-bulk-ntp").value.trim();
        if (!server) { toast("Indiquez le serveur de temps à appliquer", "error"); return; }
        if (!window.confirm("Appliquer le serveur de temps « " + server + " » sur "
            + selected.size + " machine(s) ?")) return;
        await runBulk({ action: "clock", ids: Array.from(selected), server, enabled: true });
        await loadClockAll(true);
        if (view === "overview") loadOverview();
    }

    async function runBulk(body) {
        try {
            const data = await ctx.api("bulk", { body });
            renderReport(data.results || []);
            refresh();
            if (selId) loadDetail(true);
        } catch (e) { toast(e.message, "error"); }
    }

    function renderReport(results) {
        const host = $("#hd-report");
        if (!host) return;
        host.hidden = false;
        const ko = results.filter((r) => !r.ok).length;
        // Compte-rendu LIGNE À LIGNE : une action groupée réussit presque toujours
        // partiellement, et un verdict global masquerait les machines restées à l'arrêt.
        host.innerHTML = '<div class="hd-report-h"><strong>Compte-rendu</strong>'
            + '<span class="hd-meta">' + (results.length - ko) + " sur " + results.length
            + " machine(s) — " + (ko ? ko + " en échec" : "aucun échec") + "</span></div>"
            + '<table class="hd-table"><thead><tr><th>Machine</th><th>Résultat</th></tr></thead><tbody>'
            + results.map((r) => '<tr class="' + (r.ok ? "" : "ko") + '"><td>' + esc(r.name)
                + "</td><td>" + esc(r.ok ? (r.code + " " + r.text) : (r.error || "échec"))
                + "</td></tr>").join("")
            + "</tbody></table>";
    }

    // ── Vue d'ensemble ───────────────────────────────────────
    function renderOverview(rows) {
        const host = $("#hd-ov");
        if (!rows.length) {
            host.innerHTML = '<p class="hd-empty">Aucune machine.</p>';
            return;
        }
        // « Horloge » est volontairement APRÈS le timecode et distincte de lui : le
        // timecode est une donnée de montage, l'horloge date les fichiers. Les confondre
        // est l'erreur qui fait chercher une dérive au mauvais endroit.
        host.innerHTML = '<table class="hd-table hd-ov-table"><thead><tr>'
            + "<th>Machine</th><th>Groupe</th><th>État</th><th>Timecode</th><th>Horloge</th><th>Format</th>"
            + "<th>Format fichier</th><th>Entrée</th><th>Slots</th><th>Volume réseau</th><th>Restant</th>"
            + "</tr></thead><tbody>"
            + rows.map((r) => {
                const s = r.summary || {};
                // Le slot réseau porte son propre numéro, rapporté par la machine (souvent 3).
                // Il est marqué « rés. » pour qu'on ne le confonde pas avec un SSD.
                const slotTxt = (r.slots || []).map((sl) => (sl.is_network ? "rés." : "S" + sl.slot_id)
                    + " " + (SLOT_STATUS[sl.status] || sl.status || "—")).join(" · ");
                const best = (r.slots || []).find((sl) => String(sl.slot_id) === String(s.slot_id));
                return "<tr" + (s.status === "record" ? ' class="rec"' : "") + ">"
                    + "<td><strong>" + esc(r.name) + '</strong><br><span class="hd-ip">'
                    + esc(r.host) + "</span></td>"
                    + "<td>" + esc(r.group || "—") + "</td>"
                    + "<td>" + statusBadge(s) + "</td>"
                    + '<td class="hd-mono">' + esc(s.display_timecode || s.timecode || "—") + "</td>"
                    + "<td>" + clockCell(r.id) + "</td>"
                    + "<td>" + esc(s.video_format || "—") + "</td>"
                    + "<td>" + esc(s.file_format || "—") + "</td>"
                    + "<td>" + esc(s.video_input || "—") + "</td>"
                    + "<td>" + esc(slotTxt || "—") + "</td>"
                    + "<td>" + nasCell(r, s) + "</td>"
                    + "<td>" + esc((best && best.recording_time_h) || s.recording_time_h || "—") + "</td>"
                    + "</tr>";
            }).join("")
            + "</tbody></table>";
        const m = $("#hd-ov-meta");
        if (m) m.textContent = rows.length + " machine(s) · " + (live ? "suivi en direct" : "suivi figé");
    }

    // ── Volumes réseau ───────────────────────────────────────
    // La bibliothèque de chemins vit dans l'OUTIL : on saisit serveur, identifiant et
    // chemin une seule fois, puis on les applique aux machines. La matrice du bas compare
    // ce catalogue à ce que les machines ont réellement — c'est là que se voit l'écart.

    async function loadNasPaths() {
        try {
            const data = await ctx.api("nas");
            nasPaths = data.paths || [];
            renderNasList();
            renderNasPick();
        } catch (e) {
            $("#hd-nas-list").innerHTML = '<p class="hd-empty">' + esc(e.message) + "</p>";
        }
    }

    async function loadNasMatrix() {
        try {
            nasMatrix = await ctx.api("nas/matrix");
            for (const id of Array.from(nasTargets)) {
                if (!(nasMatrix.decks || []).some((d) => d.id === id)) nasTargets.delete(id);
            }
            renderNasTargets();
            renderNasGrid();
        } catch (e) {
            $("#hd-nas-grid").innerHTML = '<p class="hd-empty">' + esc(e.message) + "</p>";
        }
    }

    function renderNasList() {
        const host = $("#hd-nas-list");
        if (!nasPaths.length) {
            host.innerHTML = '<p class="hd-empty">'
                + esc(tr("plugin.hyperdeck.nas.empty",
                    "Aucun chemin — utilisez « + Ajouter un chemin ». Exemple : smb://10.10.20.10/Rushes"))
                + "</p>";
            return;
        }
        host.innerHTML = '<table class="hd-table"><thead><tr><th>Nom</th><th>Adresse</th>'
            + "<th>Identifiant</th><th>Notes</th><th></th></tr></thead><tbody>"
            + nasPaths.map((p) => "<tr><td><strong>" + esc(p.label) + "</strong></td>"
                + '<td class="hd-mono">' + esc(p.url) + "</td>"
                + "<td>" + esc(p.username || "invité")
                + (p.has_password ? ' <span class="hd-tag">mot de passe</span>' : "") + "</td>"
                + "<td>" + esc(p.notes || "—") + "</td>"
                + '<td class="hd-row-actions">'
                + '<button class="btn btn-sm" data-nact="edit" data-id="' + esc(p.id) + '" type="button">✎</button>'
                + '<button class="btn btn-sm" data-nact="del" data-id="' + esc(p.id) + '" type="button">✕</button>'
                + "</td></tr>").join("")
            + "</tbody></table>";
    }

    function renderNasPick() {
        const sel = $("#hd-nas-pick");
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = nasPaths.map((p) => '<option value="' + esc(p.id) + '">'
            + esc(p.label) + " — " + esc(p.url) + "</option>").join("");
        if (cur) sel.value = cur;
    }

    function renderNasTargets() {
        const host = $("#hd-nas-targets");
        if (!host) return;
        const decks = nasMatrix.decks || [];
        host.innerHTML = decks.map((d) => '<label class="hd-target'
            + (d.connected ? "" : " off") + '">'
            + '<input type="checkbox" data-target="' + esc(d.id) + '"'
            + (nasTargets.has(d.id) ? " checked" : "") + ">"
            + "<span>" + esc(d.name) + "</span>"
            + (d.connected ? "" : '<span class="hd-tag">hors ligne</span>')
            + (d.nas_supported === false ? '<span class="hd-tag warn">sans NAS</span>' : "")
            + "</label>").join("") || '<p class="hd-empty">Aucune machine.</p>';
        const n = $("#hd-nas-targets-n");
        if (n) n.textContent = nasTargets.size + " machine(s) cochée(s)";
    }

    function onNasTargetChange(e) {
        const id = e.target && e.target.dataset ? e.target.dataset.target : null;
        if (!id) return;
        if (e.target.checked) nasTargets.add(id); else nasTargets.delete(id);
        const n = $("#hd-nas-targets-n");
        if (n) n.textContent = nasTargets.size + " machine(s) cochée(s)";
    }

    function renderNasGrid() {
        const host = $("#hd-nas-grid");
        const paths = nasMatrix.paths || [], decks = nasMatrix.decks || [], cells = nasMatrix.cells || {};
        if (!paths.length || !decks.length) {
            host.innerHTML = '<p class="hd-empty">'
                + (paths.length ? "Aucune machine au parc." : "Ajoutez un chemin pour voir la matrice.")
                + "</p>";
            return;
        }
        // Convention Bobi.Tools : les DESTINATAIRES du réglage (les machines) en colonnes,
        // les chemins en lignes. Chaque cellule porte son libellé texte — la couleur seule
        // ne dirait pas la différence entre « signet » et « monté ».
        host.innerHTML = '<div class="hd-gridwrap"><table class="hd-table hd-nas-grid"><thead><tr>'
            + '<th class="hd-corner">chemin ▾ / machine ▸</th>'
            + decks.map((d) => '<th><div class="hd-colh">' + esc(d.name)
                + (d.mounted ? '<span class="hd-tag on">volume monté</span>'
                    : (d.connected ? "" : '<span class="hd-tag">hors ligne</span>'))
                + "</div></th>").join("")
            + "</tr></thead><tbody>"
            + paths.map((p) => "<tr><th class=\"hd-rowh\"><strong>" + esc(p.label)
                + '</strong><br><span class="hd-mono">' + esc(p.url) + "</span></th>"
                + decks.map((d) => {
                    const c = cells[p.id + "|" + d.id] || { state: "absent", title: "" };
                    const k = NAS_CELL[c.state] || NAS_CELL.absent;
                    const act = (c.state === "mounted") ? "deselect" : "add_select";
                    const can = d.connected && d.nas_supported !== false;
                    return '<td class="hd-cell ' + k.cls + '" title="' + esc(c.title) + '">'
                        + (can ? '<button class="hd-cellbtn" data-gact="' + act
                            + '" data-path="' + esc(p.id) + '" data-deck="' + esc(d.id)
                            + '" type="button" title="' + esc(c.title) + '">' + esc(k.label) + "</button>"
                            : "<span>" + esc(k.label) + "</span>")
                        + "</td>";
                }).join("") + "</tr>").join("")
            + "</tbody></table></div>"
            + '<p class="hd-meta">Cliquez une cellule pour poser le chemin sur cette machine '
            + "(ou démonter si le volume y est monté). « Signet » = mémorisé, pas monté ; "
            + "« sélectionné » = demandé mais montage non constaté ; « monté » = utilisable.</p>";
        const m = $("#hd-nas-meta");
        if (m) m.textContent = paths.length + " chemin(s) · " + decks.length + " machine(s)";
    }

    function onNasGridClick(e) {
        const b = e.target.closest ? e.target.closest("button[data-gact]") : null;
        if (!b) return;
        runNasBulk(b.dataset.gact, b.dataset.path, [b.dataset.deck]);
    }

    function onNasListClick(e) {
        const b = e.target.closest ? e.target.closest("button[data-nact]") : null;
        if (!b) return;
        if (b.dataset.nact === "edit") return showNasForm(b.dataset.id);
        if (b.dataset.nact === "del") return deleteNasPath(b.dataset.id);
    }

    function showNasForm(id) {
        nasEditing = id || null;
        const p = id ? nasPaths.find((x) => x.id === id) : null;
        $("#hd-n-label").value = p ? p.label : "";
        $("#hd-n-url").value = p ? p.url : "";
        $("#hd-n-username").value = p ? p.username : "";
        $("#hd-n-password").value = "";
        $("#hd-n-notes").value = p ? p.notes : "";
        $("#hd-n-note").textContent = p && p.has_password
            ? tr("plugin.hyperdeck.pwKept", "Mot de passe enregistré — laissez vide pour le conserver.")
            : tr("plugin.hyperdeck.nas.pwHint",
                "Sans identifiant, le HyperDeck se connecte en invité.");
        $("#hd-nas-form").hidden = false;
    }

    async function onNasFormSubmit(e) {
        if (e && e.preventDefault) e.preventDefault();
        const body = {
            label: $("#hd-n-label").value.trim(),
            url: $("#hd-n-url").value.trim(),
            username: $("#hd-n-username").value.trim(),
            password: $("#hd-n-password").value,
            notes: $("#hd-n-notes").value.trim(),
        };
        if (!body.url) { toast(tr("plugin.hyperdeck.nas.urlRequired", "Adresse du partage requise"), "error"); return; }
        if (!body.label) body.label = body.url;
        try {
            if (nasEditing) await ctx.api("nas/" + encodeURIComponent(nasEditing), { method: "PUT", body });
            else await ctx.api("nas", { body });
            $("#hd-nas-form").hidden = true;
            nasEditing = null;
            toast(tr("plugin.hyperdeck.nas.saved", "Chemin enregistré"));
            await loadNasPaths();
            loadNasMatrix();
        } catch (err) { toast(err.message, "error"); }
    }

    async function deleteNasPath(id) {
        const p = nasPaths.find((x) => x.id === id);
        if (!window.confirm(tr("plugin.hyperdeck.nas.confirmDel",
            "Retirer ce chemin de la bibliothèque de l'outil ?\nLes signets déjà posés sur les machines ne sont pas touchés.")
            + "\n" + ((p && p.url) || ""))) return;
        try {
            await ctx.api("nas/" + encodeURIComponent(id), { method: "DELETE" });
            toast(tr("plugin.hyperdeck.deleted", "Supprimé"));
            await loadNasPaths();
            loadNasMatrix();
        } catch (e) { toast(e.message, "error"); }
    }

    function applyNas() {
        const pathId = $("#hd-nas-pick").value;
        const mode = $("#hd-nas-mode").value;
        if (!pathId && mode !== "deselect") { toast("Choisissez un chemin", "error"); return; }
        if (!nasTargets.size) { toast("Cochez au moins une machine", "error"); return; }
        const p = nasPaths.find((x) => x.id === pathId);
        const label = { add_select: "enregistrer le signet et monter", add: "enregistrer le signet",
            select: "monter", remove: "retirer le signet", deselect: "démonter le volume réseau" }[mode];
        if (!window.confirm(label + " sur " + nasTargets.size + " machine(s) ?"
            + (p && mode !== "deselect" ? "\n" + p.url : ""))) return;
        runNasBulk(mode, pathId, Array.from(nasTargets));
    }

    async function runNasBulk(mode, pathId, ids) {
        try {
            const data = await ctx.api("bulk", { body: { action: "nas", mode, nas_id: pathId, ids } });
            renderNasReport(data.results || []);
            loadNasMatrix();
        } catch (e) { toast(e.message, "error"); }
    }

    function renderNasReport(results) {
        const host = $("#hd-nas-report");
        if (!host) return;
        host.hidden = false;
        const ko = results.filter((r) => !r.ok).length;
        // Même règle que les actions groupées de transport : compte-rendu ligne à ligne.
        // Le montage étant asynchrone, un « 200 ok » ne prouve pas que le volume est monté —
        // c'est la matrice, relue juste après, qui le dit.
        host.innerHTML = '<div class="hd-report-h"><strong>Compte-rendu</strong>'
            + '<span class="hd-meta">' + (results.length - ko) + " sur " + results.length
            + " machine(s) — " + (ko ? ko + " en échec" : "aucun refus")
            + " · le montage est asynchrone : vérifiez la ligne « monté » ci-dessous</span></div>"
            + '<table class="hd-table"><thead><tr><th>Machine</th><th>Résultat</th></tr></thead><tbody>'
            + results.map((r) => '<tr class="' + (r.ok ? "" : "ko") + '"><td>' + esc(r.name)
                + "</td><td>" + esc(r.ok ? (r.code + " " + r.text) : (r.error || "échec"))
                + "</td></tr>").join("")
            + "</tbody></table>";
    }

    async function rereadNas() {
        // Relit l'état réseau SUR chaque machine (I/O réelle), au lieu de se contenter du
        // cache. Utile juste après un montage, ou quand un serveur est revenu.
        const decks = nasMatrix.decks || [];
        try {
            await Promise.all(decks.filter((d) => d.connected).map((d) =>
                ctx.api("decks/" + encodeURIComponent(d.id) + "/nas?refresh=1").catch(() => null)));
            toast(tr("plugin.hyperdeck.nas.reread1", "État réseau relu sur les machines"));
            loadNasMatrix();
        } catch (e) { toast(e.message, "error"); }
    }

    // Colonne « Volume réseau » de la vue d'ensemble : QUEL NAS, et est-ce lui qui reçoit
    // l'enregistrement. Le nom vient de la bibliothèque de l'outil quand l'adresse y
    // correspond ; sinon on affiche l'adresse brute — un partage monté depuis la face
    // avant de la machine est tout aussi réel, il n'a simplement pas de nom chez nous.
    function nasCell(r, s) {
        if (!s.network_url) return "—";
        const name = r.nas_label
            ? esc(r.nas_label)
            : '<span class="hd-mono">' + esc(s.network_url) + "</span>";
        const sub = r.nas_label
            ? '<br><span class="hd-mono hd-sub">' + esc(s.network_url) + "</span>"
            : "";
        // « actif » = c'est le slot en cours : ce que la machine enregistre part là.
        const tagOn = s.on_network
            ? ' <span class="hd-tag on">' + esc(s.status === "record" ? "enregistre ici" : "slot actif") + "</span>"
            : ' <span class="hd-tag">monté</span>';
        return name + tagOn + sub;
    }

    // ── Export / import ──────────────────────────────────────
    async function doExport() {
        try {
            const data = await ctx.api("export");
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "hyperdeck-parc.json";
            a.click();
            URL.revokeObjectURL(a.href);
            toast(tr("plugin.hyperdeck.exported", "Parc exporté (sans les mots de passe)"));
        } catch (e) { toast(e.message, "error"); }
    }

    function doImport(e) {
        const file = e.target && e.target.files && e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = async () => {
            try {
                const data = JSON.parse(reader.result);
                const rep = await ctx.api("import", { body: { data } });
                toast((rep.added || 0) + " ajoutée(s), " + (rep.updated || 0) + " actualisée(s)");
                refresh();
            } catch (err) { toast(err.message, "error"); }
        };
        reader.readAsText(file);
        e.target.value = "";
    }

    // ── Petits utilitaires ───────────────────────────────────
    function yn(v) {
        if (v === undefined || v === null || v === "") return "—";
        return String(v) === "true" ? "oui" : (String(v) === "false" ? "non" : String(v));
    }

    function fmtDuration(seconds) {
        const s = parseInt(seconds, 10);
        if (isNaN(s) || s < 0) return "—";
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        return h ? (h + " h " + String(m).padStart(2, "0")) : (m + " min");
    }

    function hhmmss(at) {
        if (!at) return "—";
        const d = new Date(at * 1000);
        return String(d.getHours()).padStart(2, "0") + ":"
            + String(d.getMinutes()).padStart(2, "0") + ":"
            + String(d.getSeconds()).padStart(2, "0");
    }

    function kvText(params) {
        if (!params) return "";
        return Object.keys(params).map((k) => k + ": " + params[k]).join(" · ");
    }

    return { mount, unmount };
})();
