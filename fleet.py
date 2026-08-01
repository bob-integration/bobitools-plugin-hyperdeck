# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Parc de HyperDeck : persistance et gestion des connexions permanentes.

Un fichier dans le volume `/data` : `decks.json` (nom, hôte, port, identifiants, groupe).
Rien d'autre n'est stocké — l'état d'un HyperDeck vit dans l'appareil, et le cacher sur
disque ne ferait qu'inventer une seconde vérité qui se désynchronise.

## Une connexion permanente par machine

Contrairement aux outils qui interrogent un appareil à chaque requête (ptz, switch_ports),
ici la connexion EST la session : l'override du mode « remote » ne survit pas à sa
fermeture, et l'appareil n'accepte qu'un petit nombre de clients simultanés. Le
gestionnaire ci-dessous tient donc un `HyperDeckClient` vivant par machine activée, et le
recycle quand l'adresse ou les identifiants changent.

## Concurrence

Règle héritée de switch_ports : le verrou disque n'est JAMAIS tenu pendant une I/O réseau.
Ici c'est presque gratuit — les lectures d'état tapent dans le cache alimenté par les
notifications, pas sur le réseau — mais les commandes de transport, elles, attendent
vraiment l'appareil.
"""
import json
import os
import threading
import time
import uuid

import protocol

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DECKS_FILE = os.path.join(DATA_DIR, "decks.json")
NAS_FILE = os.path.join(DATA_DIR, "nas.json")


def _env_num(name, default, lo, hi):
    try:
        return max(lo, min(hi, float(os.environ.get(name) or default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


REFRESH_INTERVAL = _env_num("REFRESH_INTERVAL", 20, 5, 300)
CONNECT_TIMEOUT = _env_num("CONNECT_TIMEOUT", 4, 1, 30)
LIVE_TIMECODE = _env_bool("LIVE_TIMECODE", True)

_io = threading.Lock()


# --------------------------------------------------------------------------- persistance

def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)       # atomique sur POSIX : jamais de parc à moitié écrit


def load_decks():
    with _io:
        d = _read(DECKS_FILE, [])
        return d if isinstance(d, list) else []


def save_decks(decks):
    with _io:
        _write(DECKS_FILE, decks)


def find(decks, deck_id):
    return next((d for d in decks if d.get("id") == deck_id), None)


def get(deck_id):
    return find(load_decks(), deck_id)


def groups():
    return sorted({(d.get("group") or "").strip() for d in load_decks() if (d.get("group") or "").strip()})


def new_id():
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ chemins réseau (NAS)
# Bibliothèque de chemins tenue par L'OUTIL, indépendante des machines. Elle existe parce
# qu'un HyperDeck ne sait mémoriser un partage que pour lui-même : sans elle, il faudrait
# retaper serveur, identifiant et chemin sur chaque machine, à chaque fois. Ici on saisit
# une fois, on applique où l'on veut.
#
# Ce n'est PAS un miroir de ce que les machines ont : c'est le catalogue de ce qu'on veut
# pouvoir poser. La comparaison entre les deux est justement ce que montre la matrice.

def load_nas():
    with _io:
        d = _read(NAS_FILE, [])
        return d if isinstance(d, list) else []


def save_nas(paths):
    with _io:
        _write(NAS_FILE, paths)


def find_nas(nas_id):
    return next((p for p in load_nas() if p.get("id") == nas_id), None)


def nas_public(p):
    """Vue d'un chemin destinée au navigateur : jamais le mot de passe, seulement le fait
    qu'il est renseigné."""
    return {"id": p.get("id"), "label": p.get("label") or p.get("url"),
            "url": p.get("url"), "username": p.get("username") or "",
            "has_password": bool(p.get("password")), "notes": p.get("notes") or ""}


# --------------------------------------------------------------------------- connexions

class Manager:
    """Maintient un client vivant par machine activée du parc.

    `sync()` est l'unique point d'entrée : on l'appelle après toute modification du parc et
    périodiquement. Il compare la configuration de chaque client à celle du parc et ne
    recycle que ce qui a réellement changé — recréer un client à chaque passage couperait
    la connexion (et l'override remote avec elle) toutes les dix secondes.
    """

    def __init__(self):
        self._clients = {}                  # {deck_id: HyperDeckClient}
        self._keys = {}                     # {deck_id: signature de connexion}
        self._lock = threading.Lock()

    @staticmethod
    def _key(deck):
        return (deck.get("host") or "", int(deck.get("port") or protocol.PORT),
                deck.get("user") or "", deck.get("password") or "",
                bool(deck.get("enabled", True)))

    def sync(self):
        decks = load_decks()
        wanted = {d["id"]: d for d in decks if d.get("id") and d.get("host")}
        with self._lock:
            for deck_id in list(self._clients):
                d = wanted.get(deck_id)
                if d is None or not d.get("enabled", True) or self._keys.get(deck_id) != self._key(d):
                    self._clients.pop(deck_id).stop()
                    self._keys.pop(deck_id, None)
            for deck_id, d in wanted.items():
                if not d.get("enabled", True) or deck_id in self._clients:
                    continue
                c = protocol.HyperDeckClient(
                    host=d["host"], port=d.get("port") or protocol.PORT,
                    user=d.get("user"), password=d.get("password"),
                    connect_timeout=CONNECT_TIMEOUT, refresh_interval=REFRESH_INTERVAL,
                    live_timecode=LIVE_TIMECODE, name=d.get("name") or d["host"])
                c.start()
                self._clients[deck_id] = c
                self._keys[deck_id] = self._key(d)

    def client(self, deck_id):
        with self._lock:
            return self._clients.get(deck_id)

    def require(self, deck_id):
        c = self.client(deck_id)
        if not c:
            raise protocol.HyperDeckError("machine désactivée ou sans adresse")
        return c

    def state(self, deck_id):
        """État d'une machine, ou un état « désactivé » explicite. Jamais None : une carte
        sans état ne se distinguerait pas d'une carte en cours de chargement."""
        c = self.client(deck_id)
        if not c:
            s = protocol.HyperDeckClient._blank_state()
            s["error"] = "désactivée"
            return s
        return c.state()

    def reconnect(self, deck_id):
        """Coupe la connexion : la boucle du client la rétablit immédiatement. Sert quand
        un HyperDeck est revenu et qu'on ne veut pas attendre la fin du recul."""
        with self._lock:
            c = self._clients.pop(deck_id, None)
            self._keys.pop(deck_id, None)
        if c:
            c.stop()
        self.sync()

    def stop_all(self):
        with self._lock:
            for c in self._clients.values():
                c.stop()
            self._clients.clear()
            self._keys.clear()


MANAGER = Manager()


# --------------------------------------------------------------------------- horloge
# L'horloge ne passe PAS par la connexion permanente : elle vit dans une autre interface
# (HTTP /admin/api/v1, cf. admin.py), qui n'émet aucune notification. Chaque lecture est
# donc un aller-retour réseau — d'où ce cache, pour qu'afficher la vue d'ensemble toutes
# les quelques secondes n'aille pas questionner tout le parc à chaque fois.
CLOCK_TTL = _env_num("CLOCK_TTL", 30, 5, 600)

_clock = {}                       # deck_id -> (lu_à, rapport)
_clock_lock = threading.Lock()


def clock_cached(deck_id, max_age=None):
    """Dernier rapport d'horloge, ou None s'il est trop vieux. `max_age=0` force la
    relecture ; None applique le TTL."""
    ttl = CLOCK_TTL if max_age is None else max_age
    with _clock_lock:
        entry = _clock.get(deck_id)
    if not entry:
        return None
    read_at, report = entry
    if ttl is not None and (time.time() - read_at) > ttl:
        return None
    return report


def clock_store(deck_id, report):
    with _clock_lock:
        _clock[deck_id] = (time.time(), report)
    return report


def clock_last(deck_id):
    """Dernier rapport connu, PÉRIMÉ OU NON, avec son âge : la vue d'ensemble préfère
    afficher une heure d'il y a une minute en le disant, plutôt qu'une case vide."""
    with _clock_lock:
        entry = _clock.get(deck_id)
    if not entry:
        return None
    read_at, report = entry
    return dict(report, age=round(time.time() - read_at, 1))


def start_manager():
    """Ouvre les connexions et les tient ouvertes. Une exception ne doit jamais arrêter la
    boucle : un parc entier injoignable reste un parc à surveiller."""
    def loop():
        while True:
            try:
                MANAGER.sync()
            except Exception as e:                      # noqa: BLE001
                print("hyperdeck : synchronisation du parc en échec : %s" % e, flush=True)
            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()


# --------------------------------------------------------------------------- export / import
# Le parc s'exporte en JSON SANS mot de passe (garde-fou anti-secret du projet). L'import
# fait un UPSERT par hôte : il ajoute et actualise, ne supprime jamais, et préserve le mot
# de passe d'une machine déjà connue — que l'export, par construction, ne transporte pas.

def export_data():
    return {"decks": [{k: v for k, v in d.items() if k != "password"} for d in load_decks()],
            "nas": [{k: v for k, v in p.items() if k != "password"} for p in load_nas()]}


def import_data(data):
    incoming = (data or {}).get("decks") or []
    added, updated = 0, 0
    nas_added, nas_updated = 0, 0
    with _io:
        cur = _read(DECKS_FILE, [])
        if not isinstance(cur, list):
            cur = []
        by_host = {(d.get("host") or "").strip(): d for d in cur}
        for it in incoming:
            h = (it.get("host") or "").strip()
            if not h:
                continue
            existing = by_host.get(h)
            if existing:
                pw = existing.get("password")
                keep_id = existing.get("id") or uuid.uuid4().hex[:12]
                # L'`id` du fichier importé est écarté au même titre que le mot de passe :
                # une machine déjà connue GARDE son identité. La laisser changer casserait
                # les références qui la désignent (sélections, chemins réseau appliqués)
                # et pourrait la faire entrer en collision avec une autre entrée.
                existing.update({k: v for k, v in it.items() if k not in ("password", "id")})
                existing["id"] = keep_id
                if pw:
                    existing["password"] = pw
                updated += 1
            else:
                rec = {k: v for k, v in it.items() if k != "password"}
                # Un id importé n'est repris QUE s'il est libre. Deux entrées de même id
                # font diverger les lectures du parc : `find()` rend la première, la
                # synchronisation du gestionnaire garde la dernière — et c'est cette
                # dernière qui fixe l'hôte du client TCP. L'écran afficherait alors une
                # machine et la commande partirait sur une autre.
                wanted = rec.get("id")
                if not wanted or any(d.get("id") == wanted for d in cur):
                    rec["id"] = uuid.uuid4().hex[:12]
                cur.append(rec)
                by_host[h] = rec
                added += 1
        _write(DECKS_FILE, cur)

        # Chemins réseau : upsert par URL, même règle que pour les machines — le mot de
        # passe d'un chemin déjà connu est préservé, puisque l'export ne le transporte pas.
        nas_in = (data or {}).get("nas") or []
        if nas_in:
            cur_nas = _read(NAS_FILE, [])
            if not isinstance(cur_nas, list):
                cur_nas = []
            by_url = {(p.get("url") or "").strip(): p for p in cur_nas}
            for it in nas_in:
                u = (it.get("url") or "").strip()
                if not u:
                    continue
                ex = by_url.get(u)
                if ex:
                    pw = ex.get("password")
                    ex.update({k: v for k, v in it.items() if k != "password"})
                    ex["id"] = ex.get("id") or uuid.uuid4().hex[:12]
                    if pw:
                        ex["password"] = pw
                    nas_updated += 1
                else:
                    rec = {k: v for k, v in it.items() if k != "password"}
                    rec["id"] = rec.get("id") or uuid.uuid4().hex[:12]
                    cur_nas.append(rec)
                    by_url[u] = rec
                    nas_added += 1
            _write(NAS_FILE, cur_nas)
    MANAGER.sync()
    return {"added": added, "updated": updated,
            "nas_added": nas_added, "nas_updated": nas_updated}
