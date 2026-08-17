# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Serveur de l'outil « HyperDeck » (runtime=docker).

HTTP minimal (ThreadingHTTPServer, stdlib), proxifié par l'app sous /api/tools/hyperdeck/*.

## Lire ne coûte rien, écrire coûte un aller-retour

Toutes les routes de LECTURE (`/decks`, `/overview`, `/decks/<id>`) servent le cache
alimenté par les notifications de l'appareil : aucune I/O réseau, donc l'UI peut se
rafraîchir toutes les deux secondes sans peser sur le parc ni sur le réseau. Seules les
routes d'ÉCRITURE et les listes de médias (`/clips`, `/timeline`, qu'on ne peut pas
recevoir spontanément) parlent réellement aux machines.

## Un refus n'est pas une panne

Le HyperDeck refuse explicitement : « 111 remote control disabled », « 102 invalid value »,
« 105 no disk ». Ces réponses sont rendues telles quelles, code et texte compris, en 200 —
elles décrivent l'état de la machine, pas un échec de l'outil. Seule l'impossibilité de
dialoguer (machine injoignable, connexion coupée) donne une 502.

## Actions groupées

Raison d'être de l'outil en exploitation : lancer l'enregistrement sur six machines d'un
coup. Deux règles héritées de ptz :
- **exécution en parallèle** — pour que les REC partent ensemble, pas en file indienne ;
- **compte-rendu par machine, jamais un verdict global** — annoncer « OK » alors que deux
  machines sur six n'ont pas démarré serait le pire service à rendre en direct.
"""
import json
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import admin
import ember
import fleet
import protocol
import schema
import supports

PORT = 8080
BULK_WORKERS = 12           # assez pour qu'un REC groupé parte ensemble sur un gros parc

# Champs du parc modifiables par l'UI (liste blanche : rien d'autre n'entre en base).
FIELDS = ("name", "host", "port", "user", "password", "group", "notes", "enabled")


# --------------------------------------------------------------------------- helpers

def _fmt_duration(seconds):
    """« 2 h 14 » / « 47 min » — durée d'enregistrement restante, lisible d'un coup d'œil.
    Les secondes n'ont aucun intérêt ici et ne feraient que clignoter."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if s < 0:
        return None
    h, m = divmod(s // 60, 60)
    return ("%d h %02d" % (h, m)) if h else ("%d min" % m)


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _summary(state):
    """Résumé d'une machine pour les vues de parc : ce qu'on veut voir sans cliquer."""
    tr = state.get("transport") or {}
    slots = state.get("slots") or {}
    active = str(tr.get("slot id") or "")
    slot = slots.get(active) or {}
    # Le slot réseau ne figure pas dans `slots` : il n'est pas énuméré par `slot count` et
    # ne se lit que par `slot info: device: network`. Son numéro dépend du modèle — on le
    # prend donc TEL QUE la machine le rapporte, jamais en le supposant.
    net = ((state.get("nas") or {}).get("network_slot")) or {}
    net_id = str(net.get("slot id") or "")
    on_network = bool(net_id) and active == net_id
    if on_network and not slot:
        slot = net             # le slot actif EST le volume réseau : durée restante, volume…
    return {
        "connected": state.get("connected"),
        "error": state.get("error"),
        "model": state.get("model"),
        "status": tr.get("status"),
        "speed": tr.get("speed"),
        "timecode": tr.get("timecode"),
        "display_timecode": tr.get("display timecode"),
        "video_format": tr.get("video format"),
        "input_video_format": tr.get("input video format"),
        "clip_id": tr.get("clip id"),
        "loop": tr.get("loop"),
        "single_clip": tr.get("single clip"),
        "reference_locked": tr.get("reference locked"),
        "slot_id": tr.get("slot id"),
        "slot_status": slot.get("status"),
        "volume_name": slot.get("volume name"),
        "recording_time": slot.get("recording time"),
        "recording_time_h": _fmt_duration(slot.get("recording time")),
        # Enregistre-t-on sur un volume RÉSEAU, et sur lequel ? `network_url` n'est renseigné
        # que si le volume est effectivement monté : un partage sélectionné mais jamais monté
        # ne doit pas apparaître comme la destination d'un enregistrement.
        "network_slot_id": net_id or None,
        "on_network": on_network,
        "network_url": net.get("url") if net.get("status") == "mounted" else None,
        "network_volume": net.get("volume name") if net.get("status") == "mounted" else None,
        "remote_enabled": (state.get("remote") or {}).get("enabled"),
        "remote_override": (state.get("remote") or {}).get("override"),
        "file_format": (state.get("configuration") or {}).get("file format"),
        "video_input": (state.get("configuration") or {}).get("video input"),
        "record_prefix": (state.get("configuration") or {}).get("record prefix"),
        "updated_at": state.get("updated_at"),
    }


def _public(deck, state=None):
    """Vue d'une machine destinée au navigateur. Le mot de passe n'en sort JAMAIS : on
    n'expose qu'un booléen disant qu'il est renseigné."""
    if state is None:
        state = fleet.MANAGER.state(deck.get("id"))
    return {
        "id": deck.get("id"),
        "name": deck.get("name") or deck.get("host"),
        "host": deck.get("host"),
        "port": deck.get("port") or protocol.PORT,
        "user": deck.get("user") or "",
        "has_password": bool(deck.get("password")),
        "group": deck.get("group") or "",
        "notes": deck.get("notes") or "",
        "enabled": deck.get("enabled", True),
        "summary": _summary(state),
    }


def _apply_fields(deck, body):
    for f in FIELDS:
        if f not in body:
            continue
        v = body[f]
        if f == "port":
            try:
                deck[f] = int(v) if v not in (None, "") else protocol.PORT
            except (TypeError, ValueError):
                deck[f] = protocol.PORT
        elif f == "enabled":
            deck[f] = bool(v)
        elif f == "password":
            # Une chaîne vide venant de l'UI signifie « ne change rien » : le mot de passe
            # n'est jamais renvoyé au navigateur, il ne peut donc pas être reposté tel quel.
            if v:
                deck[f] = str(v)
        else:
            deck[f] = ("" if v is None else str(v)).strip()
    return deck


def _resp_json(resp):
    """Réponse protocolaire rendue au navigateur, telle quelle."""
    return {"code": resp.code, "text": resp.text, "ok": resp.ok,
            "lines": resp.lines, "params": resp.params}


def _clock_for(deck, force=False):
    """Rapport d'horloge d'une machine, en passant par le cache.

    L'adresse est celle du parc : l'API d'administration écoute sur le port 80 de la même
    machine que le protocole 9993, mais c'est bien un autre service — une machine peut
    parfaitement répondre au protocole et refuser l'accès web (réglage « Accès réseau »),
    auquel cas l'horloge est simplement indisponible et on le dit."""
    deck_id = deck.get("id")
    if not force:
        cached = fleet.clock_cached(deck_id)
        if cached is not None:
            return cached
    report = admin.clock_report(deck.get("host") or "", time.time())
    return fleet.clock_store(deck_id, report)


def _clock_all(force=False):
    """Horloge de TOUTES les machines activées, lues en parallèle. Une machine muette ne
    doit pas retarder les autres : chacune porte son propre échec."""
    decks = [d for d in fleet.load_decks() if d.get("enabled", True)]
    out = [None] * len(decks)

    def work(i, deck):
        try:
            rep = _clock_for(deck, force=force)
        except Exception as e:                          # noqa: BLE001
            rep = {"available": False, "error": "erreur interne : %s" % e}
        out[i] = {"id": deck.get("id"), "name": deck.get("name") or deck.get("host"),
                  "host": deck.get("host"), "group": deck.get("group") or "", "clock": rep}

    for start in range(0, len(decks), BULK_WORKERS):
        batch = [(i, decks[i]) for i in range(start, min(start + BULK_WORKERS, len(decks)))]
        threads = [threading.Thread(target=work, args=(i, d), daemon=True) for i, d in batch]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    return [r for r in out if r]


def _run_bulk(deck_ids, fn):
    """Exécute `fn(deck, client)` sur chaque machine EN PARALLÈLE et rend un compte-rendu
    ligne à ligne. Un échec est rattaché à SA machine, jamais au lot."""
    decks = fleet.load_decks()
    targets = [d for d in decks if d.get("id") in set(deck_ids or [])]
    rows = [None] * len(targets)

    def work(i, deck):
        row = {"id": deck.get("id"), "name": deck.get("name") or deck.get("host"),
               "ok": False, "error": None, "code": None, "text": None}
        try:
            resp = fn(deck, fleet.MANAGER.require(deck["id"]))
            if resp is not None:
                row.update(code=resp.code, text=resp.text, ok=resp.ok)
                if not resp.ok:
                    row["error"] = "%d %s" % (resp.code, resp.text)
            else:
                row["ok"] = True
        except protocol.HyperDeckError as e:
            row["error"] = str(e)
        except Exception as e:                          # noqa: BLE001
            row["error"] = "erreur interne : %s" % e
        rows[i] = row

    # Paquets de `BULK_WORKERS` : on n'ouvre pas deux cents fils d'un coup, mais à
    # l'intérieur d'un paquet les commandes partent bien SIMULTANÉMENT — c'est tout
    # l'intérêt d'un REC groupé.
    for start in range(0, len(targets), BULK_WORKERS):
        batch = [(i, targets[i]) for i in range(start, min(start + BULK_WORKERS, len(targets)))]
        threads = [threading.Thread(target=work, args=(i, d), daemon=True) for i, d in batch]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    return [r for r in rows if r]


# --------------------------------------------------------------------------- transport

def _transport_command(action, body):
    """Traduit une action de l'UI en commande protocolaire. Rend None si l'action est
    inconnue — le serveur répond alors 400 plutôt que d'envoyer n'importe quoi."""
    v = protocol.esc_value
    if action == "play":
        parts = []
        if body.get("speed") not in (None, ""):
            parts.append("speed: %s" % int(body["speed"]))
        if "loop" in body:
            parts.append("loop: %s" % ("true" if body["loop"] else "false"))
        if "single_clip" in body:
            parts.append("single clip: %s" % ("true" if body["single_clip"] else "false"))
        return "play: " + " ".join(parts) if parts else "play"
    if action == "stop":
        return "stop"
    if action == "record":
        name = v(body.get("name"))
        return ("record: name: %s" % name) if name else "record"
    if action == "preview":
        return "preview: enable: %s" % ("true" if body.get("enable") else "false")
    if action == "remote":
        # `override` prend la main sur l'état « remote » local de la machine, mais seulement
        # le temps de la connexion — d'où l'intérêt d'une connexion permanente.
        if "override" in body:
            return "remote: override: %s" % ("true" if body["override"] else "false")
        return "remote: enable: %s" % ("true" if body.get("enable") else "false")
    if action == "identify":
        return "identify: enable: %s" % ("true" if body.get("enable", True) else "false")
    if action == "slot":
        return "slot select: slot id: %s" % int(body.get("slot_id") or 1)
    if action == "goto":
        target = v(body.get("target"))
        kind = body.get("kind") or "timeline"
        if kind == "clip_id":
            return "goto: clip id: %s" % target
        if kind == "timecode":
            return "goto: timecode: %s" % target
        if kind == "clip":
            return "goto: clip: %s" % target
        return "goto: timeline: %s" % target
    if action == "jog":
        return "jog: timecode: %s" % v(body.get("target"))
    if action == "shuttle":
        return "shuttle: speed: %s" % int(body.get("speed") or 0)
    if action == "reboot":
        return "reboot"
    return None


# --------------------------------------------------------------------------- serveur HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                        # les logs applicatifs suffisent

    # -- sortie --------------------------------------------------------------

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail500(self, exc):
        """Dernier rempart : journalise la TRACE (sinon les 500 sont muets et
        indiagnosticables) puis renvoie une 500 avec le message."""
        print("hyperdeck : 500 sur %s %s : %r" % (self.command, self.path, exc), flush=True)
        traceback.print_exc()
        return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _deck_or_404(self, deck_id):
        deck = fleet.get(deck_id)
        if not deck:
            self._send(404, {"error": "machine inconnue"})
            return None
        return deck

    # -- GET -----------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        query = parse_qs(u.query)
        try:
            if parts == ["health"]:
                return self._send(200, {"ok": True})
            if parts == ["config"]:
                return self._send(200, {
                    "refresh_interval": fleet.REFRESH_INTERVAL,
                    "connect_timeout": fleet.CONNECT_TIMEOUT,
                    "live_timecode": fleet.LIVE_TIMECODE,
                    "default_port": protocol.PORT,
                    "sections": schema.sections(),
                })
            if parts == ["ember", "tree"]:
                return self._ember_tree()
            if parts == ["decks"]:
                return self._list()
            if parts == ["overview"]:
                return self._overview()
            if parts == ["export"]:
                return self._send(200, fleet.export_data())
            if parts == ["nas"]:
                return self._send(200, {"paths": [fleet.nas_public(p) for p in fleet.load_nas()]})
            if parts == ["nas", "matrix"]:
                return self._nas_matrix()
            if parts == ["selection"]:
                ids = [i for i in (query.get("ids") or [""])[0].split(",") if i]
                return self._selection(ids)
            if parts == ["clock"]:
                # `refresh=1` : on redemande à toutes les machines. Sans ça, l'affichage
                # se contente du cache — l'horloge n'a pas besoin d'être relue à la seconde.
                force = (query.get("refresh") or [""])[0] in ("1", "true")
                return self._send(200, {"rows": _clock_all(force=force),
                                        "server_time": time.time(),
                                        "ttl": fleet.CLOCK_TTL})
            if len(parts) == 2 and parts[0] == "decks":
                deck = self._deck_or_404(parts[1])
                return None if not deck else self._detail(deck)
            if len(parts) == 3 and parts[0] == "decks":
                deck = self._deck_or_404(parts[1])
                if not deck:
                    return None
                if parts[2] == "clips":
                    return self._clips(deck, query)
                if parts[2] == "timeline":
                    return self._timeline(deck)
                if parts[2] == "commands":
                    return self._send(200, {"commands": fleet.MANAGER.state(deck["id"])["commands"]})
                if parts[2] == "nas":
                    return self._deck_nas(deck, query)
                if parts[2] == "clock":
                    return self._send(200, {"clock": _clock_for(deck, force=True),
                                            "server_time": time.time()})
            return self._send(404, {"error": "route inconnue"})
        except protocol.HyperDeckError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:                       # noqa: BLE001 — dernier rempart
            return self._fail500(e)

    # -- POST / PUT / DELETE -------------------------------------------------

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        try:
            if parts == ["decks"]:
                return self._create(body)
            if parts == ["import"]:
                return self._send(200, {"ok": True,
                                        **fleet.import_data(body.get("data") if "data" in body else body)})
            if parts == ["ember", "set"]:
                return self._ember_set(body)
            if parts == ["bulk"]:
                return self._bulk(body)
            if parts == ["nas"]:
                return self._nas_create(body)
            if len(parts) == 3 and parts[0] == "decks":
                deck = self._deck_or_404(parts[1])
                if not deck:
                    return None
                return self._deck_action(deck, parts[2], body)
            return self._send(404, {"error": "route inconnue"})
        except protocol.HyperDeckError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    def do_PUT(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        try:
            if len(parts) == 2 and parts[0] == "decks":
                return self._update(parts[1], body)
            if len(parts) == 2 and parts[0] == "nas":
                return self._nas_update(parts[1], body)
            return self._send(404, {"error": "route inconnue"})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    def do_DELETE(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if len(parts) == 2 and parts[0] == "decks":
                return self._delete(parts[1])
            if len(parts) == 2 and parts[0] == "nas":
                return self._nas_delete(parts[1])
            return self._send(404, {"error": "route inconnue"})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    # -- parc ----------------------------------------------------------------

    def _list(self):
        decks = fleet.load_decks()
        return self._send(200, {"decks": [_public(d) for d in decks],
                                "groups": fleet.groups()})

    def _create(self, body):
        host = (body.get("host") or "").strip()
        if not host:
            return self._send(400, {"error": "adresse IP requise"})
        decks = fleet.load_decks()
        deck = _apply_fields({"id": fleet.new_id(), "enabled": True,
                              "port": protocol.PORT}, body)
        deck.setdefault("name", host)
        if not deck.get("name"):
            deck["name"] = host
        decks.append(deck)
        fleet.save_decks(decks)
        fleet.MANAGER.sync()
        return self._send(200, {"ok": True, "deck": _public(deck)})

    def _update(self, deck_id, body):
        decks = fleet.load_decks()
        deck = fleet.find(decks, deck_id)
        if not deck:
            return self._send(404, {"error": "machine inconnue"})
        _apply_fields(deck, body)
        fleet.save_decks(decks)
        fleet.MANAGER.sync()
        return self._send(200, {"ok": True, "deck": _public(deck)})

    def _delete(self, deck_id):
        decks = fleet.load_decks()
        deck = fleet.find(decks, deck_id)
        if not deck:
            return self._send(404, {"error": "machine inconnue"})
        decks = [d for d in decks if d.get("id") != deck_id]
        fleet.save_decks(decks)
        fleet.MANAGER.sync()                # ferme la connexion de la machine retirée
        return self._send(200, {"ok": True})

    # -- chemins réseau (bibliothèque de l'outil) ----------------------------

    NAS_FIELDS = ("label", "url", "username", "password", "notes")

    def _nas_apply_fields(self, path, body):
        for f in self.NAS_FIELDS:
            if f not in body:
                continue
            v = body[f]
            if f == "password":
                if v:                       # vide = « ne change rien » (jamais renvoyé à l'UI)
                    path[f] = str(v)
            else:
                path[f] = ("" if v is None else str(v)).strip()
        return path

    def _nas_create(self, body):
        url = (body.get("url") or "").strip()
        if not url:
            return self._send(400, {"error": "URL du partage requise (ex. smb://serveur/partage)"})
        paths = fleet.load_nas()
        if any(_norm_url(p.get("url")) == _norm_url(url) for p in paths):
            return self._send(400, {"error": "ce chemin est déjà dans la bibliothèque"})
        path = self._nas_apply_fields({"id": fleet.new_id()}, body)
        if not path.get("label"):
            path["label"] = url
        paths.append(path)
        fleet.save_nas(paths)
        return self._send(200, {"ok": True, "path": fleet.nas_public(path)})

    def _nas_update(self, nas_id, body):
        paths = fleet.load_nas()
        path = next((p for p in paths if p.get("id") == nas_id), None)
        if not path:
            return self._send(404, {"error": "chemin inconnu"})
        self._nas_apply_fields(path, body)
        fleet.save_nas(paths)
        return self._send(200, {"ok": True, "path": fleet.nas_public(path)})

    def _nas_delete(self, nas_id):
        paths = fleet.load_nas()
        if not any(p.get("id") == nas_id for p in paths):
            return self._send(404, {"error": "chemin inconnu"})
        # Retirer un chemin de la bibliothèque ne touche PAS aux machines : les signets déjà
        # posés y restent. Les enlever d'autorité démonterait un volume en cours d'usage.
        fleet.save_nas([p for p in paths if p.get("id") != nas_id])
        return self._send(200, {"ok": True})

    def _nas_matrix(self):
        """Matrice chemins × machines : ce qui est réglé côté réseau, d'un seul écran.

        Convention Bobi.Tools : machines (destinataires du réglage) en COLONNES, chemins
        en LIGNES."""
        paths = [fleet.nas_public(p) for p in fleet.load_nas()]
        raw = {p["id"]: p for p in fleet.load_nas()}
        decks, cells = [], {}
        for d in fleet.load_decks():
            state = fleet.MANAGER.state(d["id"])
            nas = state.get("nas") or {}
            slot = nas.get("network_slot") or {}
            decks.append({
                "id": d["id"], "name": d.get("name") or d.get("host"),
                "host": d.get("host"), "group": d.get("group") or "",
                "enabled": d.get("enabled", True),
                "connected": state.get("connected"),
                "nas_supported": nas.get("supported"),
                "mounted_url": slot.get("url"),
                "mounted": slot.get("status") == "mounted",
                "volume_name": slot.get("volume name"),
                "recording_time_h": _fmt_duration(slot.get("recording time")),
            })
            for p in paths:
                cells["%s|%s" % (p["id"], d["id"])] = _nas_cell(raw[p["id"]], state)
        return self._send(200, {"paths": paths, "decks": decks, "cells": cells})

    def _deck_nas(self, deck, query):
        """État réseau d'UNE machine, tel qu'elle le rapporte. `discover=1` déclenche en
        plus la recherche mDNS — une question qu'on pose quand on cherche un serveur, pas
        à chaque rafraîchissement."""
        c = fleet.MANAGER.require(deck["id"])
        if (query.get("discover") or [""])[0] in ("1", "true"):
            c.read_nas(discover=True)
        elif (query.get("refresh") or [""])[0] in ("1", "true"):
            c.read_nas()
        return self._send(200, {"nas": c.state().get("nas")})

    def _deck_nas_action(self, client, body):
        """Pose ou retire un chemin sur UNE machine.

        `add_select` enchaîne signet puis montage : c'est le geste réellement voulu quand
        on « applique un chemin ». Les faire séparément obligerait à deux clics dont le
        premier ne produit rien de visible."""
        action = body.get("action")
        if action == "deselect":
            return {"deselect": _resp_json(client.nas_action("deselect", None))}
        path = None
        if body.get("nas_id"):
            path = fleet.find_nas(body["nas_id"])
            if not path:
                raise protocol.HyperDeckError("chemin inconnu dans la bibliothèque")
        url = (path or {}).get("url") or body.get("url")
        if not url:
            raise protocol.HyperDeckError("URL du partage requise")
        user = (path or {}).get("username") or body.get("username")
        pwd = (path or {}).get("password") or body.get("password")
        out = {}
        if action in ("add", "add_select"):
            out["add"] = _resp_json(client.nas_action("add", url, user, pwd))
        if action == "remove":
            out["remove"] = _resp_json(client.nas_action("remove", url))
        if action in ("select", "add_select"):
            out["select"] = _resp_json(client.nas_action("select", url))
        if not out:
            raise protocol.HyperDeckError("action réseau inconnue")
        return out

    # -- lecture d'état ------------------------------------------------------

    def _selection(self, ids):
        """Réglages FUSIONNÉS d'un lot de machines.

        Le principe de l'écran d'édition multiple : montrer les mêmes champs que pour une
        machine seule, en distinguant ce qui est commun de ce qui diverge. Un champ
        identique partout s'affiche avec sa valeur ; un champ qui diffère s'affiche vide
        et porte le détail (quelle valeur sur quelles machines), pour qu'on sache
        exactement ce qu'on écrase avant d'écrire.

        Une machine déconnectée n'a pas de configuration lue : elle est comptée à part et
        ne fait PAS diverger un champ — l'ignorer serait mentir, mais la traiter comme une
        valeur vide ferait apparaître des divergences qui n'existent pas."""
        decks = [d for d in fleet.load_decks() if d.get("id") in set(ids or [])]
        if not decks:
            return self._send(400, {"error": "aucune machine sélectionnée"})

        rows, missing = [], []
        for d in decks:
            st = fleet.MANAGER.state(d["id"])
            conf = st.get("configuration") or {}
            entry = {"id": d["id"], "name": d.get("name") or d.get("host"),
                     "connected": bool(st.get("connected")), "conf": conf,
                     "summary": _summary(st)}
            rows.append(entry)
            if not conf:
                missing.append(entry["name"])

        described = [r for r in rows if r["conf"]]
        merged = []
        if described:
            # L'union des clés, pas l'intersection : un réglage que seules certaines
            # machines exposent (modèles différents) doit rester visible, en disant sur
            # combien de machines il existe.
            seen = {}
            for r in described:
                for p in schema.describe(r["conf"]):
                    seen.setdefault(p["key"], dict(p, _decks=[]))
            for r in described:
                for p in schema.describe(r["conf"]):
                    seen[p["key"]]["_decks"].append((r["name"], p["value"]))
            for key, p in seen.items():
                pairs = p.pop("_decks")
                values = {}
                for name, val in pairs:
                    values.setdefault(str(val), []).append(name)
                same = len(values) == 1 and len(pairs) == len(described)
                choices = list(p.get("choices") or [])
                for val in values:
                    if choices and val not in choices:
                        choices.append(val)
                merged.append(dict(
                    p, choices=choices, same=same,
                    value=(pairs[0][1] if same else None),
                    present=len(pairs), total=len(described),
                    values=[{"value": v, "decks": names, "count": len(names)}
                            for v, names in sorted(values.items(), key=lambda kv: -len(kv[1]))]))
            order = {p["key"]: i for i, p in enumerate(schema.describe(described[0]["conf"]))}
            merged.sort(key=lambda p: (order.get(p["key"], 999), p["label"]))

        return self._send(200, {
            "decks": [{k: v for k, v in r.items() if k != "conf"} for r in rows],
            "settings": merged,
            "sections": schema.sections(),
            "unread": missing,
        })

    def _detail(self, deck):
        state = fleet.MANAGER.state(deck["id"])
        armed = supports.armed(deck["id"])
        return self._send(200, {
            "deck": _public(deck, state),
            "state": state,
            "settings": schema.describe(state.get("configuration")),
            "sections": schema.sections(),
            # Supports résolus en POSITIONS : l'écran n'a pas plus que le contrôleur à
            # raisonner en numéros de slot, qui bougent avec le disque externe sélectionné.
            "supports": supports.resolve(state),
            "filesystems": list(protocol.FILESYSTEMS),
            "format": {
                "armed": bool(armed),
                "support": (armed or {}).get("key"),
                "filesystem": (armed or {}).get("filesystem"),
                "name": (armed or {}).get("name"),
                "expires_in": round(armed["expires_at"] - time.time()) if armed else 0,
                "last_result": supports.last_result(deck["id"]),
            },
        })

    def _overview(self):
        """Une ligne par machine : le tableau qu'on laisse affiché pendant un tournage."""
        rows = []
        paths = fleet.load_nas()            # lu UNE fois pour tout le tableau
        for d in fleet.load_decks():
            state = fleet.MANAGER.state(d["id"])
            s = _summary(state)
            # Une ligne par POSITION du catalogue, jamais par clé de rangement : celle d'un
            # disque externe non sélectionné est « dev:usb512 », qui n'a rien à faire sous
            # les yeux d'un exploitant. Le catalogue donne aussi les positions VIDES, ce que
            # l'énumération des slots ne faisait pas — un lecteur de carte sans carte est
            # une information utile en tournage.
            slots = [{
                "slot_id": sup["slot_id"],
                "label": sup["label"],
                "key": sup["key"],
                "status": sup["status"],
                "volume_name": sup["volume_name"],
                "recording_time": sup["recording_time"],
                "recording_time_h": _fmt_duration(sup["recording_time"]),
                "blocked": sup["blocked"],
                "active": sup["active"],
                "is_network": sup["key"] == "net",
                "url": sup["url"],
            } for sup in supports.resolve(state)]
            rows.append({"id": d["id"], "name": d.get("name") or d.get("host"),
                         "host": d.get("host"), "group": d.get("group") or "",
                         "enabled": d.get("enabled", True), "summary": s, "slots": slots,
                         "nas_label": _nas_label(s.get("network_url"), paths)})
        return self._send(200, {"rows": rows})

    def _clips(self, deck, query):
        """Médias présents sur un disque. Nécessite un aller-retour : le protocole ne
        pousse pas spontanément le contenu des disques."""
        c = fleet.MANAGER.require(deck["id"])
        slot_id = (query.get("slot") or [None])[0]
        cmd = "disk list: slot id: %s" % int(slot_id) if slot_id else "disk list"
        resp = c.command(cmd, timeout=15)
        return self._send(200, {"response": _resp_json(resp),
                                "clips": _parse_indexed(resp.lines)})

    def _timeline(self, deck):
        c = fleet.MANAGER.require(deck["id"])
        resp = c.command("clips get", timeout=15)
        return self._send(200, {"response": _resp_json(resp),
                                "clips": _parse_indexed(resp.lines)})

    # -- actions -------------------------------------------------------------

    def _deck_action(self, deck, action, body):
        deck_id = deck["id"]
        if action == "reconnect":
            fleet.MANAGER.reconnect(deck_id)
            return self._send(200, {"ok": True})
        if action == "refresh":
            c = fleet.MANAGER.require(deck_id)
            c.refresh()
            return self._send(200, {"ok": True, "state": c.state()})

        # L'horloge se règle par l'API d'administration (HTTP), pas par le protocole
        # Ethernet : on la traite AVANT d'exiger une connexion 9993. Une machine dont le
        # protocole Ethernet est désactivé — c'est un réglage de la machine — doit rester
        # réglable, et une machine dont l'horloge dérive est justement une machine qu'on
        # veut pouvoir corriger même quand le reste va mal.
        if action == "clock":
            return self._clock_write(deck, body)

        c = fleet.MANAGER.require(deck_id)

        if action == "command":
            cmd = protocol.esc_value(body.get("command"))
            if not cmd:
                return self._send(400, {"error": "commande vide"})
            return self._send(200, {"response": _resp_json(c.command(cmd, timeout=15))})

        if action == "settings":
            return self._settings_write(c, body)

        if action == "nas":
            return self._send(200, {"results": self._deck_nas_action(c, body),
                                    "nas": c.state().get("nas")})

        if action in ("format", "format-confirm", "format-cancel"):
            return self._format_action(deck_id, c, action, body)

        if action == "support":
            # Rendre un support actif. Peut demander deux commandes (sélection du disque
            # externe puis choix du slot) et peut désélectionner un autre disque externe :
            # l'avertissement est remonté tel quel, jamais avalé.
            try:
                responses, warn = supports.activate(c, body.get("support"))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"responses": [_resp_json(r) for r in responses],
                                    "warning": warn,
                                    "supports": supports.resolve(c.state())})

        if action == "transport":
            cmd = _transport_command(body.get("action"), body)
            if not cmd:
                return self._send(400, {"error": "action inconnue"})
            resp = c.command(cmd, timeout=15)
            if resp.ok:
                # Relecture immédiate de ce que l'action a pu changer. Les notifications
                # arrivent d'elles-mêmes, mais rendre la main avec l'état d'AVANT ferait
                # clignoter l'UI (bouton REC enfoncé, statut encore « stopped » pendant
                # deux secondes) — et toutes les machines n'émettent pas une notification
                # pour tout.
                try:
                    c.read_transport()
                    if body.get("action") == "remote":
                        c.read_remote()
                    elif body.get("action") == "slot":
                        c.refresh_slots()
                except protocol.HyperDeckError:
                    pass
            return self._send(200, {"response": _resp_json(resp), "state": c.state()})

        return self._send(404, {"error": "action inconnue"})

    # -- Ember+ --------------------------------------------------------------

    def _ember_tree(self):
        """Sous-arbre servi depuis le CACHE, sans une seule I/O vers les machines.

        Le service ré-agrège toutes les cinq secondes avec un délai de garde de dix : douze
        machines interrogées en synchrone ici feraient déborder ce délai, et le sous-arbre
        entier serait remplacé par son dernier état connu — un parc figé chez le contrôleur
        sans qu'aucune erreur ne le dise."""
        decks = ember.ensure_indexes()
        return self._send(200, {"label": ember.LABEL,
                                "nodes": ember.build_nodes(decks, fleet.MANAGER.state)})

    def _ember_set(self, body):
        ref = body.get("ref") or {}
        deck_id = ref.get("deck")
        deck = fleet.get(deck_id)
        if not deck:
            return self._send(404, {"error": "machine inconnue"})
        try:
            client = fleet.MANAGER.require(deck_id)
            out = ember.apply_set(ref, body.get("value"), client, deck_id)
        except supports.Refused as e:
            return self._send(409, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        # Les réponses protocolaires sont sérialisées ici plutôt que dans `ember.py`, qui
        # n'a pas à connaître la forme rendue au réseau.
        if "response" in out:
            out["response"] = _resp_json(out["response"])
        if "responses" in out:
            out["responses"] = [_resp_json(r) for r in out["responses"]]
        return self._send(200, {"ok": True, **out})

    def _format_action(self, deck_id, client, action, body):
        """Formatage en deux temps. Le seul geste irréversible de tout l'outil.

        `format` arme et ne détruit rien ; `format-confirm` exécute ; `format-cancel`
        oublie la préparation. L'annulation n'est qu'un oubli CÔTÉ OUTIL : la machine ne
        sait pas retirer un jeton qu'elle a émis, aucune commande ne le permet, et elle
        l'accepterait encore un quart d'heure plus tard (mesuré). C'est précisément pour
        ça que la péremption tenue ici est la seule barrière réelle."""
        try:
            if action == "format":
                out = supports.prepare(deck_id, client, body.get("support"),
                                       body.get("filesystem") or protocol.FILESYSTEMS[0],
                                       body.get("name"))
            elif action == "format-confirm":
                out = supports.confirm(deck_id, client, body.get("token"))
            else:
                out = {"cancelled": supports.disarm(deck_id)}
        except supports.Refused as e:
            return self._send(409, {"error": str(e)})
        armed = supports.armed(deck_id)
        return self._send(200, {**out,
                                "armed": bool(armed),
                                "expires_in": round(armed["expires_at"] - time.time())
                                if armed else 0,
                                "supports": supports.resolve(client.state()),
                                "last_result": supports.last_result(deck_id)})

    def _clock_write(self, deck, body):
        """Règle le serveur de temps d'UNE machine, puis relit son état.

        La machine accepte n'importe quelle chaîne sans broncher : c'est la relecture qui
        dira si elle se synchronise. On la rend donc toujours, y compris quand l'état est
        « en attente » — l'appelant saura que l'écriture est passée mais que le résultat
        n'est pas encore acquis."""
        host = deck.get("host") or ""
        try:
            state = admin.set_ntp(host, body.get("server"), _bool(body.get("enabled")))
        except admin.AdminError as e:
            return self._send(502, {"error": str(e)})
        report = _clock_for(deck, force=True)
        return self._send(200, {"ntp": state, "clock": report, "server_time": time.time()})

    def _settings_write(self, client, body):
        """Écrit un ou plusieurs réglages, UN PAR COMMANDE.

        Le protocole accepte de les grouper, mais une seule valeur refusée ferait alors
        échouer tout le lot sans dire laquelle : l'utilisateur verrait « 102 invalid value »
        sans savoir quel réglage a été rejeté, et sans savoir lesquels sont passés. Une
        commande par réglage donne un compte-rendu exact."""
        values = body.get("values")
        if not isinstance(values, dict) or not values:
            return self._send(400, {"error": "aucun réglage à écrire"})
        results = []
        for key, value in values.items():
            k = protocol.esc_value(key)
            v = protocol.esc_value(value)
            if isinstance(value, bool):
                v = "true" if value else "false"
            resp = client.command("configuration: %s: %s" % (k, v), timeout=15)
            results.append({"key": key, "value": v, **_resp_json(resp)})
            if resp.code == 213:
                # « deck rebooting » : la machine ferme la connexion. Inutile d'enchaîner —
                # les commandes suivantes tomberaient dans le vide et remonteraient des
                # erreurs de connexion qui masqueraient la vraie cause.
                break
        try:
            client.read_configuration()     # l'écriture faite, on affiche ce que la machine dit
        except protocol.HyperDeckError:
            pass
        return self._send(200, {"results": results, "state": client.state()})

    def _bulk(self, body):
        """Action sur plusieurs machines à la fois. `action` reprend le vocabulaire de
        `_transport_command`, plus `settings` pour appliquer un réglage au lot."""
        action = body.get("action")
        ids = body.get("ids") or []
        if not ids:
            return self._send(400, {"error": "aucune machine sélectionnée"})

        if action == "clock":
            # Le même serveur de temps sur tout le parc : c'est le geste qui répare
            # réellement une dérive, puisqu'une machine mal synchronisée date mal ses
            # fichiers. `_run_bulk` passe par la connexion 9993 ; l'horloge, elle, tient
            # à l'API d'administration, d'où cette boucle parallèle à part.
            decks = [d for d in fleet.load_decks() if d.get("id") in set(ids)]
            rows = [None] * len(decks)
            server, enabled = body.get("server"), _bool(body.get("enabled"))

            def apply_clock(i, deck):
                row = {"id": deck.get("id"), "name": deck.get("name") or deck.get("host"),
                       "ok": False, "error": None}
                try:
                    state = admin.set_ntp(deck.get("host") or "", server, enabled)
                    row.update(ok=True, state=state.get("state"),
                               text=state.get("state_label"))
                    fleet.clock_store(deck.get("id"), admin.clock_report(
                        deck.get("host") or "", time.time()))
                except admin.AdminError as e:
                    row["error"] = str(e)
                except Exception as e:                  # noqa: BLE001
                    row["error"] = "erreur interne : %s" % e
                rows[i] = row

            for start in range(0, len(decks), BULK_WORKERS):
                batch = [(i, decks[i]) for i in range(start, min(start + BULK_WORKERS, len(decks)))]
                threads = [threading.Thread(target=apply_clock, args=(i, d), daemon=True)
                           for i, d in batch]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()
            return self._send(200, {"results": [r for r in rows if r]})

        if action == "settings":
            values = body.get("values") or {}
            if not values:
                return self._send(400, {"error": "aucun réglage à écrire"})

            def apply_settings(deck, client):
                last = None
                keys = list(values)
                for idx, key in enumerate(keys):
                    value = values[key]
                    v = "true" if isinstance(value, bool) and value else (
                        "false" if isinstance(value, bool) else protocol.esc_value(value))
                    last = client.command("configuration: %s: %s" % (
                        protocol.esc_value(key), v), timeout=15)
                    if last.code == 213:
                        # « deck rebooting » : la machine redémarre et ferme la connexion.
                        # 213 est un 2xx, donc `ok` vaut True et le seul test `if not last.ok`
                        # laissait la boucle continuer : les réglages suivants partaient dans
                        # une socket morte et la HyperDeckError qui s'ensuivait faisait compter
                        # la machine « en échec de connexion », masquant à la fois la vraie
                        # cause et le fait que le premier réglage, lui, était bien passé.
                        # On s'arrête donc ici, comme le chemin mono-machine (`_settings_write`),
                        # et on NOMME les réglages non appliqués : sur un lot, l'opérateur doit
                        # savoir quoi rejouer après le redémarrage sans avoir à tout réappliquer
                        # (ce qui relancerait un redémarrage de tout le parc).
                        reste = keys[idx + 1:]
                        if reste:
                            last = protocol.Response(
                                last.code,
                                "%s — réglages non appliqués (à rejouer après redémarrage) : %s"
                                % (last.text, ", ".join(reste)), [])
                        break
                    if not last.ok:
                        return last
                return last

            return self._send(200, {"results": _run_bulk(ids, apply_settings)})

        if action == "nas":
            # Le geste qui motive toute la rubrique : poser le même chemin réseau sur N
            # machines. Le compte-rendu porte la PREMIÈRE réponse en échec de la séquence
            # (add puis select) — annoncer le succès du signet alors que le montage a
            # échoué serait exactement le contraire du service rendu.
            nas_body = dict(body)
            nas_body["action"] = body.get("mode") or "add_select"

            def apply_nas(deck, client):
                out = self._deck_nas_action(client, nas_body)
                last = None
                for step in ("add", "remove", "select", "deselect"):
                    r = out.get(step)
                    if not r:
                        continue
                    last = protocol.Response(r["code"], "%s : %s" % (step, r["text"]), [])
                    if not r["ok"]:
                        return last
                return last

            return self._send(200, {"results": _run_bulk(ids, apply_nas)})

        cmd = _transport_command(action, body)
        if not cmd:
            return self._send(400, {"error": "action inconnue"})
        return self._send(200, {"results": _run_bulk(
            ids, lambda deck, client: client.command(cmd, timeout=15))})


def _norm_url(u):
    """URL normalisée pour COMPARER un chemin de la bibliothèque à ce qu'une machine
    rapporte. On se limite au strictement sûr : espaces, casse du schéma et de l'hôte,
    barre oblique finale. Pas de résolution DNS, pas d'équivalence nom↔IP : déclarer
    identiques `smb://nas.local/X` et `smb://10.0.0.5/X` serait une supposition, et la
    matrice afficherait « monté » là où rien ne l'est."""
    u = (u or "").strip().rstrip("/")
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/]*)(.*)$", u)
    if not m:
        return u
    return m.group(1).lower() + m.group(2).lower() + m.group(3)


def _nas_label(url, paths):
    """Nom lisible du chemin de la bibliothèque correspondant à `url`, ou None.

    Rend None — et pas l'URL — quand rien ne correspond : l'appelant affiche alors l'URL
    brute. Inventer un nom pour un partage que l'outil ne connaît pas laisserait croire
    qu'il vient de la bibliothèque, donc qu'il a été posé depuis ici."""
    if not url:
        return None
    target = _norm_url(url)
    for p in paths or []:
        if _norm_url(p.get("url")) == target:
            return p.get("label") or p.get("url")
    return None


def _nas_cell(path, state):
    """État du croisement (chemin, machine). Quatre faits distincts, jamais confondus :

    `mounted`   le slot réseau porte cette URL et est monté — le volume est utilisable ;
    `selected`  la machine l'a sélectionné mais le montage n'est pas (encore) constaté ;
    `bookmark`  un signet existe, rien n'est monté ;
    `absent`    la machine ne connaît pas ce chemin.

    Distinguer `selected` de `mounted` n'est pas un raffinement : `nas select` est
    asynchrone, et un serveur injoignable laisse un partage sélectionné indéfiniment sans
    qu'il soit monté. Les afficher pareil ferait croire à un volume disponible qui ne
    l'est pas — exactement l'erreur qui coûte un enregistrement."""
    if not state.get("connected"):
        return {"state": "offline", "title": "machine hors ligne"}
    nas = state.get("nas") or {}
    if nas.get("supported") is False:
        return {"state": "unsupported", "title": "firmware sans commandes NAS"}
    target = _norm_url(path.get("url"))
    slot = nas.get("network_slot") or {}
    slot_url = _norm_url(slot.get("url"))
    if target and slot_url == target and (slot.get("status") == "mounted"):
        return {"state": "mounted",
                "title": "monté · %s" % (slot.get("volume name") or slot.get("url") or "")}
    for e in (nas.get("selected") or []):
        if target and _norm_url(e.get("url")) == target:
            return {"state": "selected",
                    "title": "sélectionné, montage non constaté (serveur injoignable ?)"}
    for e in (nas.get("bookmarks") or []):
        if target and _norm_url(e.get("url")) == target:
            return {"state": "bookmark", "title": "signet enregistré, non monté"}
    return {"state": "absent", "title": "absent de cette machine"}


def _parse_indexed(lines):
    """Lignes « {index}: {champs…} » d'une liste de médias, rendues telles quelles.

    On ne découpe PAS les champs : leur nombre et leur ordre dépendent de la version de
    réponse négociée et du modèle, et un nom de fichier contient des espaces. Découper à
    l'aveugle produirait des colonnes fausses ; l'index et le reste de la ligne, eux, sont
    toujours justes."""
    out = []
    for line in lines:
        if ":" not in line:
            continue
        idx, rest = line.split(":", 1)
        idx = idx.strip()
        if not idx.isdigit():
            continue
        out.append({"index": int(idx), "text": rest.strip()})
    return out


def main():
    fleet.start_manager()
    print("hyperdeck : serveur sur 0.0.0.0:%d (données : %s)" % (PORT, fleet.DATA_DIR),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
