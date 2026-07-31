# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""API d'administration du HyperDeck (HTTP, port 80) — l'horloge et le NTP.

## Pourquoi ce module existe

Le HyperDeck Ethernet Protocol (TCP 9993) ne sait RIEN de l'horloge : sur un Studio 4K Pro
en firmware 9.0.2, `clock`, `time`, `date` et `ntp` répondent tous « 100 syntax error », et
aucune des 135 commandes déclarées par `commands` ne touche à l'heure. L'API REST de
contrôle (`/control/api/v1`, 70 points d'entrée décrits par la machine elle-même) l'ignore
tout autant.

L'horloge vit dans une TROISIÈME interface, servie sur le port 80 sous `/admin/api/v1/` :
celle qu'utilise l'utilitaire « Blackmagic HyperDeck Setup ». Blackmagic ne la documente
nulle part ; ce module est écrit d'après l'observation du dialogue réel entre Setup et une
machine (capture réseau, 2026-07-31). Les formats ci-dessous sont donc RELEVÉS, pas
supposés — mais ils n'ont aucune garantie de stabilité d'un firmware à l'autre, d'où la
tolérance systématique aux champs manquants.

## Ce qu'on en tire

    GET  /admin/api/v1/dateAndTime      {"time": "1785508625",
                                         "timeFriendly": "20260731-143705",
                                         "timezoneOffset": 0}
    GET  /admin/api/v1/dateAndTime/ntp  {"enabled": true,
                                         "serverUrl": "time.cloudflare.com",
                                         "state": "Failed"}
    PUT  /admin/api/v1/dateAndTime/ntp  {"serverUrl": "...", "enabled": true}

Toute réponse est enveloppée : `{"response": {...}, "success": true}`.

`time` est un epoch en SECONDES (rendu comme une chaîne) : c'est une date absolue, donc
comparer l'heure de la machine à celle du serveur ne dépend d'aucun fuseau — le fuseau
(`timezoneOffset`, en minutes à l'ouest, convention JavaScript) ne sert qu'à afficher
l'heure telle que la machine la voit, et donc telle qu'elle date ses fichiers.

## Attention : cette API n'exige aucune authentification

Sur le firmware observé, `/admin/api/v1/` répond sans identifiants — elle règle pourtant le
réseau, les accès et l'horloge. Cet outil s'en tient volontairement à l'horloge : le reste
(interface réseau, comptes, certificats) n'a rien à faire dans un outil de pilotage
d'enregistreurs, et une écriture malheureuse y couperait la machine du réseau.
"""
import json
import urllib.error
import urllib.request

BASE = "/admin/api/v1"
DEFAULT_TIMEOUT = 4.0

# États rapportés par la machine (relevés dans le code de l'interface d'administration).
# `Unsynchronized` est transitoire : l'interface officielle bascule sur « échec » au bout
# de quelques secondes sans réponse du serveur.
STATE_LABELS = {
    "Synchronized": "synchronisé",
    "OffsetCorrection": "correction en cours",
    "Unsynchronized": "en attente",
    "Failed": "échec",
    "Inactive": "désactivé",
}
STATE_OK = ("Synchronized", "OffsetCorrection")


class AdminError(Exception):
    """L'API d'administration n'a pas répondu, ou a refusé."""


def _call(host, path, method="GET", body=None, timeout=DEFAULT_TIMEOUT):
    url = "http://%s%s%s" % (host, BASE, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise AdminError("HTTP %s sur %s" % (e.code, path))
    except urllib.error.URLError as e:
        # Cas courant et NORMAL : l'accès web est désactivé sur la machine
        # (Réglages → Accès réseau), ou le firmware est trop ancien pour cette API.
        raise AdminError("injoignable en HTTP (%s)" % getattr(e, "reason", e))
    except OSError as e:
        raise AdminError(str(e))
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise AdminError("réponse illisible (JSON attendu)")
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise AdminError(str(payload.get("error") or "refus de la machine"))
        return payload.get("response", payload)
    return payload


def date_time(host, timeout=DEFAULT_TIMEOUT):
    """Horloge de la machine. `epoch` est absolu (UTC) ; `tz_offset_min` suit la convention
    JavaScript (minutes À L'OUEST de UTC : 0 = UTC, -120 = UTC+2)."""
    r = _call(host, "/dateAndTime", timeout=timeout)
    epoch = r.get("time")
    try:
        epoch = int(str(epoch))
    except (TypeError, ValueError):
        epoch = None
    off = r.get("timezoneOffset")
    try:
        off = int(off)
    except (TypeError, ValueError):
        off = None
    return {"epoch": epoch, "friendly": r.get("timeFriendly") or "", "tz_offset_min": off}


def ntp(host, timeout=DEFAULT_TIMEOUT):
    r = _call(host, "/dateAndTime/ntp", timeout=timeout)
    state = r.get("state") or ""
    return {"enabled": bool(r.get("enabled")), "server": r.get("serverUrl") or "",
            "state": state, "state_label": STATE_LABELS.get(state, state or "inconnu"),
            "ok": state in STATE_OK}


def set_ntp(host, server, enabled=True, timeout=DEFAULT_TIMEOUT):
    """Règle le serveur de temps. La machine ACCEPTE n'importe quelle chaîne : un nom qui
    ne résout pas est enregistré sans broncher et l'état passe simplement en échec —
    c'est `ntp()` qui le dira, pas cet appel."""
    server = (server or "").strip()
    if enabled and not server:
        raise AdminError("adresse du serveur de temps requise")
    _call(host, "/dateAndTime/ntp", method="PUT",
          body={"serverUrl": server, "enabled": bool(enabled)}, timeout=timeout)
    return ntp(host, timeout=timeout)


def clock_report(host, now_epoch, timeout=DEFAULT_TIMEOUT):
    """Vue d'horloge complète d'une machine, telle que l'affiche la vue d'ensemble.

    `drift` est la différence machine − serveur en secondes, donc indépendante des fuseaux
    (deux dates absolues). Une machine peut très bien afficher une heure JUSTE avec un NTP
    en échec : elle a été mise à l'heure un jour et n'a pas encore dérivé. C'est
    précisément le cas qu'on veut rendre visible, parce qu'il ne se voit nulle part
    ailleurs et qu'il finit toujours par se payer sur l'horodatage des fichiers."""
    out = {"available": False, "error": None, "epoch": None, "friendly": "",
           "tz_offset_min": None, "drift": None, "ntp": None, "read_at": now_epoch}
    try:
        dt = date_time(host, timeout=timeout)
    except AdminError as e:
        out["error"] = str(e)
        return out
    out["available"] = True
    out.update(dt)
    if dt.get("epoch") is not None:
        out["drift"] = dt["epoch"] - now_epoch
    try:
        out["ntp"] = ntp(host, timeout=timeout)
    except AdminError as e:
        out["ntp"] = {"enabled": None, "server": "", "state": "", "state_label": str(e),
                      "ok": False}
    return out
