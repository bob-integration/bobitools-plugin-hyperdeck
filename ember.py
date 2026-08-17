# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Sous-arbre Ember+ du parc d'enregistreurs (MODE LIBRE : `ember/tree` + `ember/set`).

## Ce que le contrôleur voit, et à quoi il s'accroche

Une machine EST un nœud, **identifié par son nom d'inventaire**. C'est un choix assumé, le
même que celui de `switch_ports` : devant un contrôleur broadcast, « Hyperdeck 3 » parle et
« 10.1.12.3 » non. Le prix est connu — mesuré sur le VSM réel, la liaison se fait par
l'IDENTIFIANT et non par le numéro de chemin, donc **renommer une machine lui fait perdre
son câblage** alors que la renuméroter est transparent. La consigne d'exploitation est donc
de figer les noms AVANT de câbler, et un doublon de nom est signalé au journal sans être
renommé d'office : un suffixe automatique casserait le câblage de celui qui était déjà là.

L'adresse ne disparaît pas pour autant, elle descend en paramètre. Elle ne doit surtout pas
occuper la DESCRIPTION du nœud : un contrôleur qui ne recopie l'identifiant qu'à description
vide afficherait alors tout le parc en adresses IP.

## Les énumérations sont des contrats, pas des listes d'affichage

L'INDEX est la valeur vue par le contrôleur. Toute liste publiée ici est donc figée, et une
valeur nouvelle s'ajoute EN FIN. Deux conséquences concrètes :

- les formats de fichier viennent de la liste FIXE de `schema.py`, jamais de ce que la
  machine annonce — sinon deux HyperDeck de firmwares différents donneraient deux
  significations au même index ;
- quand la machine annonce un format absent de la liste, on retombe sur l'index 0 (« — »)
  et la valeur réelle reste lisible dans un paramètre à part. Un repli silencieux qui
  mentirait serait pire que le trou qu'il bouche.

## Rien n'est lu sur le matériel ici

L'arbre est construit depuis le cache alimenté par les notifications. Le service Ember+
ré-agrège toutes les cinq secondes avec un délai de garde de dix : une lecture qui parlerait
aux douze machines ferait sauter le sous-arbre entier.
"""
import fleet
import protocol
import schema
import supports

LABEL = "Enregistreurs"

# Statuts de transport, ordre FIGÉ. L'index 0 vaut « inconnu » : une machine injoignable ne
# doit pas se lire « stopped », qui serait un mensonge exploitable en direct.
STATUSES = ["—", "preview", "stopped", "play", "forward", "rewind", "jog", "shuttle",
            "record"]

# États d'un support, ordre FIGÉ. « montage » est un état à part entière : sans lui, un
# disque en cours de montage se lirait « absent » pendant plusieurs secondes.
SLOT_STATES = ["absent", "vide", "montage", "monté", "erreur"]
_SLOT_STATE_MAP = {"empty": 1, "mounting": 2, "mounted": 3, "error": 4}

# Formats de fichier : liste FIGÉE, index 0 réservé à « format hors liste ».
FILE_FORMATS = ["—"] + list(schema.FILE_FORMATS)

# Destinations et cibles de formatage, index 0 réservé à « aucun choix ». Le catalogue des
# supports fixe l'ordre (cf. supports.SUPPORTS) ; le formatage exclut le volume réseau.
DESTINATIONS = ["—"] + [s["label"] for s in supports.SUPPORTS]
FORMAT_TARGETS = ["—"] + [s["label"] for s in supports.FORMATTABLE]
FILESYSTEMS = list(protocol.FILESYSTEMS)


def _enum(labels):
    return [{"value": i, "label": l} for i, l in enumerate(labels)]


def _index(labels, value, default=0):
    try:
        return labels.index(value)
    except ValueError:
        return default


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _yes(value):
    return str(value or "").strip().lower() == "true"


def _p(pid, label, ptype, value, writable=False, ref=None, enum=None):
    param = {"id": pid, "label": label, "type": ptype, "value": value,
             "writable": bool(writable)}
    if enum is not None:
        param["enum"] = enum
    if ref is not None:
        param["ref"] = ref
    return param


def _machine_node(idx, deck, state):
    """Le sous-arbre d'UNE machine."""
    did = deck.get("id")
    tr = state.get("transport") or {}
    cfg = state.get("configuration") or {}
    connected = bool(state.get("connected"))
    r = lambda field: {"deck": did, "field": field}          # noqa: E731 — lisibilité locale

    file_format = cfg.get("file format") or ""
    params = [
        _p(1, "Joignable", "bool", connected),
        _p(2, "Adresse", "string", deck.get("host") or ""),
        _p(3, "Modèle", "string", state.get("model") or ""),
        _p(4, "Statut", "enum", _index(STATUSES, tr.get("status")), enum=_enum(STATUSES)),
        _p(5, "Enregistrement", "bool", tr.get("status") == "record", True, r("record")),
        _p(6, "Nom des fichiers", "string", cfg.get("record prefix") or "", True,
           r("prefix")),
        _p(7, "Horodatage auto", "bool", _yes(cfg.get("append timestamp")), True,
           r("timestamp")),
        _p(8, "Format de fichier", "enum", _index(FILE_FORMATS, file_format), True,
           r("file_format"), _enum(FILE_FORMATS)),
        # Le format RÉEL, en clair. Quand la machine annonce un format absent de la liste
        # figée, l'énumération ci-dessus retombe sur « — » : sans ce paramètre, l'arbre
        # serait muet sur ce que la machine fait vraiment.
        _p(9, "Format de fichier (annoncé)", "string", file_format),
        _p(10, "Télécommande", "bool", _yes((state.get("remote") or {}).get("enabled")),
           True, r("remote")),
        _p(11, "Timecode", "string", tr.get("timecode") or ""),
        _p(12, "Format vidéo", "string", tr.get("video format") or ""),
        _p(13, "Format d'entrée", "string", tr.get("input video format") or ""),
        _p(14, "Référence verrouillée", "bool", _yes(tr.get("reference locked"))),
        _p(15, "Erreur", "string", state.get("error") or ""),
    ]

    resolved = supports.resolve(state)
    active = next((s for s in resolved if s["active"]), None)
    dest_params = [
        _p(1, "Support actif", "enum",
           _index(DESTINATIONS, active["label"]) if active else 0, True, r("destination"),
           _enum(DESTINATIONS)),
        _p(2, "État", "enum",
           _SLOT_STATE_MAP.get((active or {}).get("status"), 0), enum=_enum(SLOT_STATES)),
        _p(3, "Volume", "string", (active or {}).get("volume_name") or ""),
        _p(4, "Temps restant (s)", "int", _int((active or {}).get("recording_time"))),
        _p(5, "Protégé en écriture", "bool", bool((active or {}).get("blocked"))),
    ]
    # Une position par support, TOUJOURS publiée : un nœud qui disparaît quand on retire une
    # carte ferait perdre son câblage au contrôleur, et le retour de la carte ne le rendrait
    # pas. Un support absent se lit « absent », ce qui est une information, pas un trou.
    for pos, sup in enumerate(resolved, start=1):
        dest_params.extend([
            _p(10 * pos + 100, "%s — état" % sup["label"], "enum",
               _SLOT_STATE_MAP.get(sup["status"], 0), enum=_enum(SLOT_STATES)),
            _p(10 * pos + 101, "%s — volume" % sup["label"], "string", sup["volume_name"]),
            _p(10 * pos + 102, "%s — temps restant (s)" % sup["label"], "int",
               _int(sup["recording_time"])),
        ])

    armed = supports.armed(did)
    intent = supports.intent(did)
    target_label = None
    if intent.get("key"):
        target_label = next((s["label"] for s in supports.FORMATTABLE
                             if s["key"] == intent["key"]), None)
    fmt_params = [
        _p(1, "Système de fichiers", "enum",
           _index(FILESYSTEMS, intent.get("filesystem"), 0), True, r("fmt_fs"),
           _enum(FILESYSTEMS)),
        _p(2, "Support à formater", "enum", _index(FORMAT_TARGETS, target_label), True,
           r("fmt_target"), _enum(FORMAT_TARGETS)),
        _p(3, "Préparer", "bool", False, True, r("fmt_prepare")),
        # Le témoin que l'opérateur DOIT voir passer à vrai avant de pouvoir confirmer.
        # C'est lui qui fait du formatage un geste en deux temps plutôt qu'un bouton.
        _p(4, "Prêt à confirmer", "bool", bool(armed)),
        _p(5, "Confirmer", "bool", False, True, r("fmt_confirm")),
        _p(6, "Dernier résultat", "string", supports.last_result(did)),
        _p(7, "Nom du volume", "string", intent.get("name") or "", True, r("fmt_name")),
    ]

    return {"id": idx, "label": deck.get("name") or deck.get("host") or did, "desc": "",
            "params": params,
            "nodes": [{"id": 100, "label": "Destination", "params": dest_params},
                      {"id": 200, "label": "Formatage", "params": fmt_params}]}


def build_nodes(decks, state_of):
    """Les nœuds du parc. `state_of(deck_id)` sert le CACHE, sans I/O réseau."""
    nodes, idents = [], {}
    for idx, deck in _indexed(decks):
        ident = deck.get("name") or deck.get("host") or deck.get("id")
        if ident in idents:
            # Deux machines de même nom = deux nœuds de même identifiant : le contrôleur ne
            # peut plus les distinguer. On le signale sans renommer — renommer d'office
            # casserait le câblage de celle qui était là avant.
            print("hyperdeck: nom « %s » en doublon (positions %d et %d) — deux nœuds Ember+ "
                  "de même identifiant, le câblage du contrôleur devient ambigu"
                  % (ident, idents[ident], idx), flush=True)
        else:
            idents[ident] = idx
        nodes.append(_machine_node(idx, deck, state_of(deck.get("id"))))
    return nodes


def apply_set(ref, value, client, deck_id):
    """Applique une écriture venue du contrôleur. Rend un dict de compte-rendu.

    Les impulsions (« Préparer », « Confirmer ») n'agissent que sur une valeur VRAIE. C'est
    délibéré : un contrôleur réémet ses valeurs à la reconnexion et au rappel d'un
    instantané, et un booléen relu à faux ne doit surtout pas déclencher quoi que ce soit.
    Le vrai verrou du formatage reste ailleurs — le confirm exige une préparation vivante,
    que l'outil fait périmer en une minute (cf. supports.ARM_TTL_S).
    """
    field = (ref or {}).get("field")
    esc = protocol.esc_value

    if field == "record":
        return {"response": client.command("record" if value else "stop", timeout=15)}
    if field == "prefix":
        return {"response": client.command("configuration: record prefix: %s" % esc(value),
                                           timeout=15)}
    if field == "timestamp":
        return {"response": client.command(
            "configuration: append timestamp: %s" % ("true" if value else "false"),
            timeout=15)}
    if field == "remote":
        return {"response": client.command(
            "remote: enable: %s" % ("true" if value else "false"), timeout=15)}
    if field == "file_format":
        idx = _int(value)
        if idx <= 0 or idx >= len(FILE_FORMATS):
            return {"ignored": "index de format hors liste"}
        return {"response": client.command(
            "configuration: file format: %s" % esc(FILE_FORMATS[idx]), timeout=20)}
    if field == "destination":
        idx = _int(value)
        if idx <= 0 or idx > len(supports.SUPPORTS):
            return {"ignored": "aucune destination"}
        responses, warn = supports.activate(client, supports.SUPPORTS[idx - 1]["key"])
        return {"responses": responses, "warning": warn}

    # --- formatage ---
    if field == "fmt_fs":
        idx = _int(value)
        if idx < 0 or idx >= len(FILESYSTEMS):
            return {"ignored": "système de fichiers hors liste"}
        return {"intent": supports.set_intent(deck_id, filesystem=FILESYSTEMS[idx])}
    if field == "fmt_target":
        idx = _int(value)
        if idx <= 0 or idx > len(supports.FORMATTABLE):
            return {"intent": supports.set_intent(deck_id, key="")}
        return {"intent": supports.set_intent(
            deck_id, key=supports.FORMATTABLE[idx - 1]["key"])}
    if field == "fmt_name":
        return {"intent": supports.set_intent(deck_id, name=str(value or ""))}
    if field == "fmt_prepare":
        if not value:
            return {"ignored": "impulsion à faux"}
        intent = supports.intent(deck_id)
        if not intent.get("key"):
            raise supports.Refused("aucun support choisi pour le formatage")
        return supports.prepare(deck_id, client, intent["key"],
                                intent.get("filesystem") or FILESYSTEMS[0],
                                intent.get("name"))
    if field == "fmt_confirm":
        if not value:
            return {"ignored": "impulsion à faux"}
        out = supports.confirm(deck_id, client)
        # Le support choisi retombe à « — » : sans ça, un ancien câblage rejoué depuis le
        # contrôleur pourrait relancer un formatage sur la cible précédente.
        supports.set_intent(deck_id, key="")
        return out
    raise supports.Refused("champ ember inconnu : %s" % field)


def ensure_indexes():
    """Attribue une position Ember+ COLLANTE à toute machine qui n'en a pas.

    Les identifiants de machine sont tirés au hasard : les trier donnerait un ordre qui
    change à chaque ajout, donc des numéros qui bougent. Le registre suit les trois règles
    du plan de numérotation du service Ember+, pour les mêmes raisons :
      1. une position attribuée ne bouge JAMAIS ;
      2. une position libérée n'est JAMAIS réattribuée — un trou se voit, alors qu'un
         numéro recyclé donnerait en silence le câblage d'une machine à une autre ;
      3. un nouveau venu prend la plus petite position libre.

    Une machine DÉSACTIVÉE garde la sienne : la lui reprendre ferait qu'un aller-retour sur
    une case à cocher déplace un sous-arbre.
    """
    decks = fleet.load_decks()
    used = {d["ember_index"] for d in decks if isinstance(d.get("ember_index"), int)}
    changed, nxt = False, 1
    for deck in sorted(decks, key=lambda d: str(d.get("id") or "")):
        if isinstance(deck.get("ember_index"), int):
            continue
        while nxt in used:
            nxt += 1
        deck["ember_index"], changed = nxt, True
        used.add(nxt)
    if changed:
        fleet.save_decks(decks)
    return decks


def _indexed(decks):
    """[(position, machine)] des machines actives, triées par position."""
    out = [(d.get("ember_index"), d) for d in decks
           if isinstance(d.get("ember_index"), int) and d.get("enabled", True)]
    return sorted(out, key=lambda t: t[0])
