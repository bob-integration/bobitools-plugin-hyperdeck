# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Supports d'enregistrement d'un HyperDeck : catalogue, résolution, formatage armé.

## Pourquoi un catalogue, et pas la liste que rend la machine

Le HyperDeck désigne ses supports de deux façons incompatibles, et une seule est stable :

- les **lecteurs de cartes** ont un numéro de slot (1, 2) ;
- le **slot externe** — le dernier numéro, 3 sur un Studio HD Plus — n'est pas un support :
  c'est une place occupée par le disque externe SÉLECTIONNÉ, réseau ou USB-C. Un disque
  externe présent mais non sélectionné se présente `slot id: none` et n'a de nom que son
  `device` (« usb512 »), un jeton fabriqué par la machine.

Autrement dit, le numéro d'un support change selon ce qui est sélectionné, et un support
peut n'avoir aucun numéro. Publier ces numéros vers un contrôleur broadcast, qui lie son
câblage à ce qu'on lui montre, reviendrait à déplacer une destination d'enregistrement le
jour où quelqu'un bascule sur le réseau.

D'où ce catalogue à **positions figées**. Une position ne bouge jamais, elle est publiée
même quand le support est absent (un nœud qui disparaît fait perdre son câblage), et c'est
l'outil qui traduit la position en `slot id` ou en `device` au moment d'agir.

⚠ **`usb512` désigne le PORT, pas le disque.** Il a survécu à un débranchement, à un
rebranchement et à un changement de SSD ; les notifications le portent même avec
`status: empty`. Il adresse donc de façon fiable, mais ne dit jamais QUEL disque est en
place — deux disques d'un même lot sont indiscernables au protocole.
"""
import threading
import time

import protocol

# Positions FIGÉES. L'ordre EST le contrat : c'est l'index vu par un contrôleur Ember+.
# Une position nouvelle s'ajoute EN FIN DE LISTE, jamais au milieu, et une position retirée
# laisse un trou plutôt que de décaler ses voisines.
#
# `formattable` : le volume réseau en est exclu délibérément. La commande l'accepterait
# (`format: device: network`), mais c'est un partage partagé par tout un parc — l'effacer
# sur une fausse manœuvre depuis un contrôleur n'a pas le même prix qu'une carte, et il
# reste formatable depuis l'écran de l'outil, où le geste est explicite.
SUPPORTS = [
    {"key": "sd1", "label": "Carte SD 1", "slot_id": "1", "formattable": True},
    {"key": "sd2", "label": "Carte SD 2", "slot_id": "2", "formattable": True},
    {"key": "usb", "label": "SSD USB-C", "external": "usb", "formattable": True},
    {"key": "net", "label": "Réseau", "external": protocol.NETWORK_DEVICE,
     "formattable": False},
]

FORMATTABLE = [s for s in SUPPORTS if s["formattable"]]

# Péremption d'une préparation de formatage, tenue PAR L'OUTIL.
#
# La machine n'en a aucune : un jeton émis quinze minutes plus tôt a été accepté et le
# disque formaté. Un `prepare` armé puis oublié reste donc armé indéfiniment côté appareil.
# C'est intenable dès qu'un contrôleur entre dans la boucle — il réémet ses valeurs à la
# reconnexion et au rappel d'un instantané, et un « Confirmer » resté à vrai repartirait
# tout seul. L'outil refuse donc lui-même un confirm périmé, sans jamais le transmettre.
ARM_TTL_S = 60.0

_lock = threading.RLock()
_armed = {}         # {deck_id: {token, key, device, filesystem, name, at, expires_at}}
_intent = {}        # {deck_id: {filesystem, key}} — choix en cours, avant la préparation
_result = {}        # {deck_id: dernier compte-rendu de formatage, en clair}


def _slots_by_device(state):
    """Index des slots par jeton `device`. Un support sélectionné est rangé sous son
    NUMÉRO et un support non sélectionné sous « dev:<jeton> » : cet index rattrape les
    deux, puisque les deux portent le champ `device`."""
    out = {}
    for slot in (state.get("slots") or {}).values():
        dev = str(slot.get("device") or "").strip()
        if dev:
            out[dev] = slot
    return out


def _external_token(state, family):
    """Jeton `device` du disque externe de cette famille, ou None.

    « usb » n'est pas un jeton mais un préfixe : la machine fabrique « usb512 », et rien ne
    garantit ce suffixe d'un modèle à l'autre. On cherche donc par préfixe plutôt que de
    coder en dur un jeton relevé sur une machine."""
    if family == protocol.NETWORK_DEVICE:
        return protocol.NETWORK_DEVICE
    for dev in (state.get("external") or {}).get("drives") or []:
        if str(dev).startswith(family):
            return dev
    # Repli : un disque vu par une notification alors que `external drive list` n'a pas
    # encore été relu. Mieux vaut un support visible qu'un support absent à tort.
    for dev in _slots_by_device(state):
        if str(dev).startswith(family) and dev != protocol.NETWORK_DEVICE:
            return dev
    return None


def resolve(state):
    """Le catalogue confronté à l'état réel d'une machine. Une entrée par position, TOUJOURS,
    présente ou non."""
    by_dev = _slots_by_device(state)
    active_id = str(((state.get("transport") or {}).get("slot id") or "")).strip()
    out = []
    for spec in SUPPORTS:
        device, slot = None, None
        if spec.get("slot_id"):
            slot = (state.get("slots") or {}).get(spec["slot_id"]) or None
            device = (slot or {}).get("device")
        else:
            device = _external_token(state, spec["external"])
            slot = by_dev.get(device) if device else None
        status = (slot or {}).get("status")
        # « Actif » = ce sur quoi la machine enregistrerait maintenant, et une seule règle
        # suffit pour les deux familles : le support porte-t-il le numéro du slot actif ?
        #
        # Un disque externe NON sélectionné se présente `slot id: none`, qui ne peut égaler
        # aucun slot actif ; le sélectionné, lui, porte le numéro du slot externe. Inutile
        # donc de croiser avec `external drive selected` — le slot lui-même le dit déjà, et
        # s'en remettre à lui évite d'avoir tort quand cette lecture manque.
        slot_no = str((slot or {}).get("slot id") or "").strip()
        active = bool(active_id) and slot_no == active_id
        out.append({
            "key": spec["key"],
            "label": spec["label"],
            "formattable": spec["formattable"],
            "device": device,
            "slot_id": (slot or {}).get("slot id") or spec.get("slot_id"),
            "present": status in ("mounted", "mounting"),
            "status": status or "absent",
            "volume_name": (slot or {}).get("volume name") or "",
            "recording_time": (slot or {}).get("recording time"),
            "remaining_size": (slot or {}).get("remaining size"),
            "total_size": (slot or {}).get("total size"),
            "blocked": str((slot or {}).get("blocked") or "").lower() == "true",
            "url": (slot or {}).get("url") or "",
            "active": active,
        })
    return out


def find(state, key):
    """Une position résolue, par sa clé. None si la clé est inconnue du catalogue."""
    for s in resolve(state):
        if s["key"] == key:
            return s
    return None


def activate(client, key):
    """Rend ce support actif. Peut demander DEUX commandes, et le dit.

    Un disque externe ne devient la destination qu'après avoir été sélectionné parmi les
    disques externes PUIS choisi comme slot actif. Et la sélection est exclusive :
    basculer sur le SSD retire le volume réseau, ce que l'appelant doit remonter à
    l'opérateur plutôt que de le laisser découvrir en enregistrant au mauvais endroit.

    Rend `(réponses, avertissement)`.
    """
    state = client.state()
    sup = find(state, key)
    if not sup:
        raise ValueError("support inconnu : %s" % key)
    responses, warn = [], None
    spec = next(s for s in SUPPORTS if s["key"] == key)
    if not spec.get("slot_id"):
        if not sup["device"]:
            raise ValueError("aucun disque externe « %s » n'est branché" % spec["external"])
        previous = (state.get("external") or {}).get("selected")
        if previous and previous != sup["device"]:
            warn = ("le disque externe « %s » a été désélectionné : les disques externes "
                    "s'excluent" % previous)
        responses.append(client.select_external_drive(sup["device"]))
        if not responses[-1].ok:
            return responses, warn
        sup = find(client.state(), key) or sup
    target = sup.get("slot_id")
    if target and str(target) != "none":
        responses.append(client.command("slot select: slot id: %s" % protocol.esc_value(target),
                                        timeout=15))
    return responses, warn


# ------------------------------------------------------------------- formatage armé

def intent(deck_id):
    """Choix de formatage en cours pour cette machine (système de fichiers + support).

    Il vit ici et pas dans l'appareil parce qu'il n'y existe pas : le protocole n'a qu'un
    `prepare` immédiat. Un contrôleur Ember+, lui, écrit un paramètre à la fois — il lui
    faut donc un endroit où poser son choix avant de déclencher."""
    with _lock:
        return dict(_intent.get(deck_id) or {"filesystem": protocol.FILESYSTEMS[0], "key": None})


def set_intent(deck_id, **fields):
    with _lock:
        cur = dict(_intent.get(deck_id) or {"filesystem": protocol.FILESYSTEMS[0], "key": None})
        cur.update({k: v for k, v in fields.items() if v is not None})
        _intent[deck_id] = cur
        return dict(cur)


def armed(deck_id):
    """La préparation en cours, ou None si elle n'existe pas ou a expiré.

    La péremption est vérifiée À LA LECTURE, et l'entrée périmée est effacée : on ne veut
    pas d'un jeton qui traîne en mémoire et qu'un chemin oublié pourrait rejouer."""
    with _lock:
        a = _armed.get(deck_id)
        if not a:
            return None
        if time.time() >= a["expires_at"]:
            _armed.pop(deck_id, None)
            return None
        return dict(a)


def arm(deck_id, token, key, device, filesystem, name):
    with _lock:
        now = time.time()
        a = {"token": token, "key": key, "device": device, "filesystem": filesystem,
             "name": name or "", "at": now, "expires_at": now + ARM_TTL_S}
        _armed[deck_id] = a
        return dict(a)


class Refused(Exception):
    """Refus de l'OUTIL, avant tout dialogue avec la machine. Distinct d'un refus de
    l'appareil, qui remonte avec son code protocolaire."""


def prepare(deck_id, client, key, filesystem, name=None):
    """Arme un formatage, après vérification. Ne détruit rien.

    Les vérifications sont faites ICI et pas dans l'écran, parce qu'il y a désormais deux
    portes — l'UI et un contrôleur Ember+ — et qu'une barrière qui ne tient que d'un côté
    n'est pas une barrière.
    """
    spec = next((s for s in FORMATTABLE if s["key"] == key), None)
    if not spec:
        raise Refused("support « %s » non formatable depuis cet outil" % key)
    if filesystem not in protocol.FILESYSTEMS:
        raise Refused("système de fichiers inconnu : %s" % filesystem)
    vol = None
    if name:
        vol = protocol.valid_volume_name(name)
        if not vol:
            raise Refused("nom de volume invalide : ni espace ni deux-points, le protocole "
                          "couperait la commande")
    state = client.state()
    if (state.get("transport") or {}).get("status") == "record":
        raise Refused("machine en enregistrement — formatage refusé")
    sup = find(state, key)
    if not sup:
        raise Refused("support inconnu : %s" % key)
    if sup["blocked"]:
        raise Refused("support protégé en écriture — à débloquer avant de formater")

    token, resp = client.format_prepare(filesystem, device=sup["device"],
                                        slot_id=None if sup["device"] else spec.get("slot_id"),
                                        name=vol)
    if not token:
        set_last_result(deck_id, "%d %s" % (resp.code, resp.text))
        return {"ready": False, "code": resp.code, "text": resp.text}
    a = arm(deck_id, token, key, sup["device"], filesystem, vol)
    set_last_result(deck_id, "préparé : %s en %s%s" % (spec["label"], filesystem,
                                                       (" → « %s »" % vol) if vol else ""))
    return {"ready": True, "expires_in": round(a["expires_at"] - time.time()),
            "support": spec["label"], "filesystem": filesystem, "name": vol,
            "code": resp.code, "text": resp.text}


def confirm(deck_id, client, token=None):
    """Exécute le formatage armé. DESTRUCTIF.

    Le jeton attendu est celui que l'OUTIL a retenu. Un jeton fourni par l'appelant doit
    lui correspondre : accepter n'importe lequel reviendrait à faire du couple
    prepare/confirm un simple aller-retour, alors que c'est la seule chose qui sépare un
    clic d'un disque effacé.
    """
    a = armed(deck_id)
    if not a:
        raise Refused("aucune préparation en cours, ou préparation expirée — "
                      "recommencez par la préparation")
    if token and token != a["token"]:
        raise Refused("jeton ne correspondant pas à la préparation en cours")
    resp = client.format_confirm(a["token"])
    disarm(deck_id)                 # à usage unique, quel que soit le verdict
    label = next((s["label"] for s in FORMATTABLE if s["key"] == a["key"]), a["key"])
    if resp.ok:
        set_last_result(deck_id, "%s formaté en %s%s" % (
            label, a["filesystem"], (" — « %s »" % a["name"]) if a["name"] else ""))
    else:
        set_last_result(deck_id, "%d %s" % (resp.code, resp.text))
    return {"ok": bool(resp.ok), "code": resp.code, "text": resp.text, "support": label}


def last_result(deck_id):
    """Compte-rendu du dernier formatage, en clair.

    Un contrôleur broadcast n'a pas de fenêtre d'erreur : s'il ne voit pas ce qui s'est
    passé dans l'arbre, il ne le voit nulle part. Les refus du HyperDeck étant tous
    distincts (`160 invalid format`, `161 invalid token`, `150 invalid state`), on rend la
    cause réelle plutôt qu'un « échec » qui n'aide personne."""
    with _lock:
        return _result.get(deck_id) or ""


def set_last_result(deck_id, text):
    with _lock:
        _result[deck_id] = str(text or "")


def disarm(deck_id):
    """Oublie la préparation. La machine, elle, garde son jeton valide — elle ne sait pas
    l'annuler, aucune commande ne le permet. C'est bien pour ça que l'outil ne doit jamais
    perdre le sien de vue : le nôtre est la seule barrière qui existe."""
    with _lock:
        return _armed.pop(deck_id, None) is not None
