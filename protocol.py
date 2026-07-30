# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Client du « HyperDeck Ethernet Protocol » (Blackmagic Design, TCP 9993).

Protocole texte orienté ligne, documenté publiquement par Blackmagic. Trois traits
dictent toute l'architecture de ce module :

1. **L'appareil PARLE tout seul.** Une fois abonné (`notify: transport: true` …), le
   HyperDeck émet des blocs `5xx` à tout moment, entre les réponses aux commandes. On ne
   peut donc pas se contenter d'un aller-retour synchrone : il faut un lecteur permanent
   qui trie les blocs par code (5xx → cache d'état, le reste → réponse à la commande en
   cours).

2. **La connexion est LA session.** L'override du mode « remote » n'est valable que le
   temps de la connexion, et le nombre de clients simultanés est limité (« 120 connection
   failed »). Ouvrir une connexion par requête HTTP gaspillerait ce budget et perdrait
   l'override. On tient donc UNE connexion permanente par machine, reconnectée toute
   seule, et toutes les requêtes de l'outil passent par elle.

3. **Le modèle décide de tout.** Les entrées vidéo, les formats de fichier, les réglages
   présents varient d'un HyperDeck à l'autre. Ce module ne présuppose RIEN : il lit ce que
   l'appareil déclare (`commands` en XML, `configuration`) et laisse la couche du dessus
   n'afficher que ça. Une commande refusée remonte telle quelle, avec son code.

Aucune dépendance : stdlib seule.
"""
import errno
import queue
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET

PORT = 9993

# Un bloc de réponse commence par « {code} {texte} », suivi de paramètres si le texte se
# termine par « : », jusqu'à une ligne vide.
_HEADER = re.compile(r"^(\d{3})\s*(.*)$")

# Codes asynchrones documentés → rubrique du cache d'état. Les 5xx NON listés ici ne sont
# pas perdus pour autant : ils atterrissent dans `state["extra"]` avec leur libellé. Le
# protocole en ajoute au fil des firmwares (timecode affiché, position timeline, images
# perdues…) et il serait absurde de jeter une information parce qu'on n'a pas su la nommer.
ASYNC_MAP = {
    500: "connection",
    502: "slot",
    508: "transport",
    510: "remote",
    511: "configuration",
}

MAX_EVENTS = 60             # journal roulant des messages asynchrones, pour le diagnostic


class HyperDeckError(Exception):
    """Échec de dialogue : pas de connexion, pas de réponse, socket cassée."""


def _connect_reason(exc, host, port):
    """Cause de connexion traduite. Distinguer « refusée » de « sans réponse » n'est pas
    cosmétique : la première dit que la machine est là mais que le port est fermé (mauvais
    port, ou HyperDeck sans Ethernet), la seconde qu'on ne l'atteint pas du tout (éteinte,
    mauvais VLAN, mauvaise adresse). Ce ne sont pas les mêmes dépannages."""
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "sans réponse (%s:%d) — machine éteinte ou adresse injoignable" % (host, port)
    err = getattr(exc, "errno", None)
    if err == errno.ECONNREFUSED:
        return "connexion refusée sur %s:%d — port fermé ou mauvais port" % (host, port)
    if err in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "réseau injoignable (%s)" % host
    return "connexion impossible (%s:%d) : %s" % (host, port, exc)


class Response:
    """Bloc de réponse : code, texte, lignes de paramètres brutes et paires clé/valeur.

    Les deux vues sont conservées volontairement. Les réponses de listes (`disk list`,
    `clips get`) ne sont PAS des paires clé/valeur — ce sont des lignes positionnelles
    « {index}: {champs…} » — et les écraser dans un dictionnaire perdrait l'ordre et les
    doublons. `params` sert aux réponses réellement nommées (configuration, transport…),
    `lines` sert au reste.
    """

    def __init__(self, code, text, lines):
        self.code = code
        self.text = text
        self.lines = lines

    @property
    def ok(self):
        return 200 <= self.code < 300

    @property
    def params(self):
        out = {}
        for line in self.lines:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        return out

    def __repr__(self):
        return "<Response %d %s (%d lignes)>" % (self.code, self.text, len(self.lines))


def xlr_pairs(lines):
    """Types des entrées XLR, dans l'ordre, extraits des lignes brutes de `configuration`.

    Ces deux paramètres sont les seuls du protocole à être RÉPÉTÉS dans la réponse — une
    paire « xlr input id » / « xlr type » par entrée. Les lire dans le dictionnaire
    clé/valeur ne rendrait que la dernière paire : sur un HyperDeck Studio 4K Pro à quatre
    XLR, trois entrées disparaîtraient silencieusement de l'affichage. D'où cette lecture
    positionnelle, seul endroit du module où l'ordre des lignes compte.
    """
    out, cur = [], None
    for line in lines:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "xlr input id":
            if cur:
                out.append(cur)
            cur = {"id": v, "type": None}
        elif k == "xlr type" and cur:
            cur["type"] = v
    if cur:
        out.append(cur)
    return out


_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def parse_nas_entries(lines):
    """Entrées d'une réponse NAS (`nas list`, `nas selected`).

    Le protocole documente la SYNTAXE D'ÉCRITURE des commandes `nas` (url / username /
    password sur des lignes séparées) mais PAS le format exact des réponses de lecture.
    On n'invente donc aucune structure : une ligne qui contient une URL ouvre une entrée,
    et les lignes suivantes qui n'en contiennent pas lui sont rattachées **brutes**, sans
    être renommées ni typées. L'UI affiche l'URL (seule chose dont on soit sûr) et la ligne
    telle quelle à côté.

    Rattacher les lignes sans URL à l'entrée précédente reste une hypothèse — celle de
    l'ordre d'écriture documenté. Elle n'est jamais présentée comme un fait : `raw` porte
    toujours le texte d'origine, consultable tel quel.
    """
    entries, cur = [], None
    for line in lines:
        m = _URL_RE.search(line)
        if m:
            cur = {"url": m.group(0), "raw": line, "details": []}
            entries.append(cur)
        elif cur is not None:
            cur["details"].append(line)
        else:
            # Ligne avant toute URL (en-tête, compteur…) : conservée dans une entrée sans
            # URL plutôt que jetée. Rien de ce que la machine dit ne doit disparaître.
            cur = {"url": None, "raw": line, "details": []}
            entries.append(cur)
            cur = None
    return entries


def nas_payload(action, url, username=None, password=None):
    """Commande `nas` multiligne. Le protocole n'accepte PAS ces commandes sur une seule
    ligne (« 163 parameterized single line command not supported ») : url, username et
    password vont chacun sur leur ligne, et un saut de ligne vide clôt le bloc."""
    lines = ["nas %s:" % action, "url: %s" % esc_value(url)]
    if action == "add":
        if username:
            lines.append("username: %s" % esc_value(username))
        if password:
            lines.append("password: %s" % esc_value(password))
    return "\r\n".join(lines) + "\r\n\r\n"


def parse_commands_xml(lines):
    """Noms des commandes déclarées par la réponse `212 commands:` (XML).

    Sert de table des capacités : c'est ce que Blackmagic recommande pour adapter une UI
    au firmware réellement en face. Un XML illisible n'est pas une erreur fatale — on rend
    une liste vide et l'UI retombe sur « tout est possible, l'appareil arbitrera ».
    """
    try:
        root = ET.fromstring("\n".join(lines))
    except ET.ParseError:
        return []
    return sorted({c.get("name") for c in root.iter("command") if c.get("name")})


class HyperDeckClient:
    """Connexion permanente à un HyperDeck, avec cache d'état alimenté par les notifications.

    Un fil « connexion » (reconnexion + entretien) et un fil « lecteur » (découpage des
    blocs). Les commandes sont sérialisées par un verrou : l'appareil répond dans l'ordre
    où on l'interroge, donc une seule commande en vol suffit et garantit l'appariement
    réponse ↔ commande sans numéro de séquence (le protocole n'en a pas).
    """

    def __init__(self, host, port=PORT, user=None, password=None, connect_timeout=4.0,
                 refresh_interval=20.0, live_timecode=True, name=None, on_event=None):
        self.host = host
        self.port = int(port or PORT)
        self.user = user or ""
        self.password = password or ""
        self.connect_timeout = float(connect_timeout or 4)
        self.refresh_interval = max(5.0, float(refresh_interval or 20))
        self.live_timecode = bool(live_timecode)
        self.name = name or host
        self._on_event = on_event

        self._sock = None
        self._stop = threading.Event()
        self._preamble = threading.Event()
        self._cmd_lock = threading.RLock()
        self._resp = queue.Queue()
        self._state_lock = threading.Lock()
        self._reader = None
        self._conn = None

        self._block = None              # bloc 5xx/2xx en cours d'assemblage
        self._state = self._blank_state()

    # ------------------------------------------------------------------ état

    @staticmethod
    def _blank_state():
        return {
            "connected": False,
            "error": None,
            "connected_at": None,
            "updated_at": None,
            "model": None,
            "protocol_version": None,
            "device": {},           # 204 device info
            "transport": {},        # 208 / 508
            "configuration": {},    # 211 / 511
            "remote": {},           # 210 / 510
            "xlr": [],              # entrées XLR : [{id, type}] (cf. xlr_pairs)
            "nas": {                # volumes réseau réglés SUR la machine
                "supported": None,  # None = pas encore su ; False = firmware sans commandes nas
                "bookmarks": [],    # signets enregistrés dans la machine
                "selected": [],     # partage actuellement sélectionné
                "discovered": [],   # serveurs vus en mDNS (lignes brutes)
                "network_slot": {}, # slot « network » : dit si le partage est RÉELLEMENT monté
                "error": None,
                "read_at": None,
            },
            "slots": {},            # {slot id: 202 / 502}
            "notify": {},           # 209 notify
            "commands": [],         # capacités déclarées (212 commands)
            "extra": {},            # 5xx non cartographiés, par code
            "events": [],           # journal roulant
        }

    def state(self):
        """Instantané du cache. Copie superficielle par rubrique : l'appelant ne doit
        jamais tenir une référence sur une structure que le fil lecteur réécrit."""
        with self._state_lock:
            s = dict(self._state)
            for k in ("device", "transport", "configuration", "remote", "notify", "extra"):
                s[k] = dict(s[k])
            s["slots"] = {k: dict(v) for k, v in s["slots"].items()}
            s["commands"] = list(s["commands"])
            s["events"] = list(s["events"])
            s["xlr"] = [dict(x) for x in s["xlr"]]
            nas = dict(s["nas"])
            for k in ("bookmarks", "selected", "discovered"):
                nas[k] = list(nas[k])
            nas["network_slot"] = dict(nas["network_slot"])
            s["nas"] = nas
            return s

    def supports(self, command):
        """L'appareil déclare-t-il cette commande ? `True` par défaut quand la table des
        capacités n'a pas pu être lue : mieux vaut proposer une action que l'appareil
        refusera clairement que la masquer sur une supposition."""
        with self._state_lock:
            cmds = self._state["commands"]
        return (command in cmds) if cmds else True

    def _touch(self, **fields):
        with self._state_lock:
            self._state.update(fields)
            self._state["updated_at"] = time.time()

    def _log_event(self, code, text, params):
        ev = {"at": time.time(), "code": code, "text": text, "params": params}
        with self._state_lock:
            self._state["events"].insert(0, ev)
            del self._state["events"][MAX_EVENTS:]

    # ------------------------------------------------------------ cycle de vie

    def start(self):
        if self._conn and self._conn.is_alive():
            return
        self._stop.clear()
        self._conn = threading.Thread(target=self._connection_loop, daemon=True,
                                      name="hyperdeck-%s" % self.host)
        self._conn.start()

    def stop(self):
        self._stop.set()
        self._close_socket()

    def _close_socket(self):
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _connection_loop(self):
        """Connecte, entretient, reconnecte. Ne s'arrête que sur `stop()`.

        Le recul entre deux tentatives croît jusqu'à 30 s : un HyperDeck éteint pour la
        nuit ne doit pas provoquer une tentative par seconde pendant douze heures, mais
        une machine qui revient doit être reprise en moins d'une demi-minute.
        """
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as e:                      # noqa: BLE001 — jamais fatal
                self._touch(connected=False, error=str(e))
            finally:
                self._close_socket()
                self._preamble.clear()
                with self._state_lock:
                    self._state["connected"] = False
            if self._stop.wait(backoff):
                return
            backoff = min(30.0, backoff * 1.7)

    def _session(self):
        """Une session complète : connexion, préambule, abonnements, entretien."""
        try:
            sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        except OSError as e:
            # « [Errno 111] Connection refused » finit affiché sur une carte de l'outil :
            # autant dire ce que ça veut dire pour l'exploitant, qui n'a pas à connaître
            # les codes errno.
            raise HyperDeckError(_connect_reason(e, self.host, self.port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(1.0)            # réveil régulier du lecteur pour honorer stop()
        self._sock = sock
        self._block = None
        while not self._resp.empty():   # une session neuve ne réutilise jamais l'ancienne file
            self._resp.get_nowait()

        self._reader = threading.Thread(target=self._read_loop, args=(sock,), daemon=True)
        self._reader.start()

        # Le préambule « 500 connection info » arrive spontanément. Sans lui, on n'est pas
        # devant un HyperDeck (ou la connexion a été refusée) : inutile d'aller plus loin.
        if not self._preamble.wait(self.connect_timeout + 2):
            raise HyperDeckError("pas de préambule — est-ce bien un HyperDeck ?")

        self._touch(connected=True, error=None, connected_at=time.time())
        try:
            self._handshake()
        except HyperDeckError as e:
            self._touch(error="connecté, mais l'interrogation initiale a échoué : %s" % e)

        # Entretien : ping régulier (détecte une socket morte que TCP n'a pas encore vue)
        # et relecture complète périodique (filet de sécurité si une notification s'est
        # perdue — l'état affiché ne doit jamais rester faux indéfiniment).
        last_refresh = time.monotonic()
        while not self._stop.is_set() and self._reader.is_alive():
            if self._stop.wait(5.0):
                return
            if not self._reader.is_alive():
                break
            try:
                self.command("ping", timeout=6)
            except HyperDeckError as e:
                raise HyperDeckError("connexion perdue (%s)" % e)
            if time.monotonic() - last_refresh >= self.refresh_interval:
                last_refresh = time.monotonic()
                try:
                    self.refresh()
                except HyperDeckError:
                    pass
        raise HyperDeckError("connexion fermée par l'appareil")

    def _handshake(self):
        """Interrogation initiale + abonnements.

        Les abonnements sont posés UN PAR UN. Groupés, un seul paramètre inconnu du
        firmware (« 101 unsupported parameter ») ferait tomber toute la commande et on se
        retrouverait sans aucune notification — panne silencieuse, l'outil affichant un
        état figé sans jamais dire pourquoi. Un par un, un refus ne coûte que la rubrique
        concernée.
        """
        resp = self.command("device info")
        if resp.ok:
            dev = resp.params
            self._touch(device=dev, model=dev.get("model"),
                        protocol_version=dev.get("protocol version"))

        resp = self.command("commands", timeout=8)
        if resp.ok:
            self._touch(commands=parse_commands_xml(resp.lines))

        wanted = ["transport", "slot", "remote", "configuration", "dropped frames", "nas"]
        if self.live_timecode:
            wanted += ["display timecode", "timeline position"]
        for topic in wanted:
            try:
                self.command("notify: %s: true" % topic, timeout=4)
            except HyperDeckError:
                break                                   # connexion partie : inutile d'insister
        resp = self.command("notify")
        if resp.ok:
            self._touch(notify=resp.params)

        self.refresh()

    # Lectures unitaires. Chacune interroge ET met à jour le cache : une lecture qui
    # renverrait la réponse sans l'y verser laisserait l'UI afficher l'état d'avant
    # l'action qu'on vient de commander — exactement le clignotement qu'on cherche à
    # éviter en relisant.

    def read_transport(self):
        resp = self.command("transport info")
        if resp.ok:
            self._touch(transport=resp.params)
        return resp

    def read_configuration(self):
        resp = self.command("configuration")
        if resp.ok:
            self._set_configuration(resp.lines, resp.params)
        return resp

    def read_remote(self):
        resp = self.command("remote")
        if resp.ok:
            self._touch(remote=resp.params)
        return resp

    def read_nas(self, discover=False):
        """Lit ce qui est réglé côté RÉSEAU sur la machine : signets, partage sélectionné,
        et surtout l'état RÉEL du slot réseau.

        Les trois ne disent pas la même chose, et les confondre serait le piège de cette
        rubrique :
          - un **signet** est une entrée mémorisée, rien de plus — il ne monte rien ;
          - un partage **sélectionné** a été demandé, mais `nas select` est ASYNCHRONE : si
            le serveur ne répond pas, il reste sélectionné sans jamais être monté ;
          - seul le **slot réseau** (`slot info: device: network`) dit que le volume est
            réellement disponible pour enregistrer et relire.
        L'UI affiche les trois séparément pour cette raison.

        `discover` (mDNS) n'est pas fait au fil de l'eau : c'est une question qu'on pose
        quand on cherche un serveur, pas toutes les vingt secondes.
        """
        with self._state_lock:
            if self._state["nas"]["supported"] is False:
                return          # firmware sans commandes nas : ne pas insister à chaque tour
        nas = {"read_at": time.time(), "error": None}
        resp = self.command("nas list", timeout=10)
        if resp.code == 103 or resp.code == 100:
            # « unsupported » / « syntax error » : cette machine ne connaît pas les volumes
            # réseau. On le retient pour ne plus l'interroger là-dessus.
            with self._state_lock:
                self._state["nas"].update(supported=False, read_at=time.time(),
                                          error="%d %s" % (resp.code, resp.text))
            return
        nas["supported"] = True
        if resp.ok:
            nas["bookmarks"] = parse_nas_entries(resp.lines)
        else:
            nas["error"] = "%d %s" % (resp.code, resp.text)

        resp = self.command("nas selected", timeout=10)
        nas["selected"] = parse_nas_entries(resp.lines) if resp.ok else []

        # Le slot réseau n'existe que si un partage est monté ; « 105 no disk » ou un refus
        # est ici une réponse NORMALE, pas une erreur à afficher en rouge.
        resp = self.command("slot info: device: network", timeout=10)
        nas["network_slot"] = resp.params if resp.ok else {}

        if discover:
            resp = self.command("nas discovered", timeout=15)
            # Réponse positionnelle non documentée (« hôte.local. NomAffiché ») : gardée en
            # lignes brutes plutôt que découpée sur une supposition.
            nas["discovered"] = list(resp.lines) if resp.ok else []

        with self._state_lock:
            self._state["nas"].update(nas)
            self._state["updated_at"] = time.time()

    def nas_action(self, action, url, username=None, password=None, timeout=20):
        """`nas add` / `nas remove` / `nas select` / `nas deselect`, puis relecture.

        `nas select` monte le partage de façon ASYNCHRONE : la réponse `200 ok` dit que la
        demande est acceptée, pas que le volume est monté. On relit donc l'état juste après,
        et l'UI montre le slot réseau — seule preuve du montage effectif."""
        if action == "deselect":
            resp = self.command("nas deselect", timeout=timeout)
        else:
            resp = self.command(nas_payload(action, url, username, password), timeout=timeout)
        try:
            self.read_nas()
        except HyperDeckError:
            pass
        return resp

    def refresh(self):
        """Relecture complète de l'état par interrogation directe.

        Sert au démarrage, périodiquement, et après toute écriture. Chaque lecture est
        indépendante : un réglage non supporté par un HyperDeck Shuttle ne doit pas
        empêcher de lire le transport."""
        self.read_transport()
        self.read_configuration()
        self.read_remote()
        self.refresh_slots()
        try:
            self.read_nas()
        except HyperDeckError:
            raise
        except Exception:                               # noqa: BLE001
            pass

    def refresh_slots(self):
        """Un `slot info` par slot déclaré par `device info`.

        Sans `slot count`, on interroge le slot actif seul : inventer un nombre de slots
        ferait afficher des slots qui n'existent pas sur un Shuttle."""
        with self._state_lock:
            count = self._state["device"].get("slot count")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            resp = self.command("slot info")
            if resp.ok:
                p = resp.params
                self._merge_slot(p.get("slot id") or "1", p)
            return
        for n in range(1, count + 1):
            resp = self.command("slot info: slot id: %d" % n)
            if resp.ok:
                self._merge_slot(str(n), resp.params)

    def _set_configuration(self, lines, params):
        """Fusionne les réglages, et ne remplace les entrées XLR que si le bloc en parle.

        Une notification `511` peut être partielle : effacer la liste des XLR parce qu'un
        message ne les mentionnait pas ferait disparaître la rubrique de l'écran à chaque
        changement de tout autre réglage."""
        xlr = xlr_pairs(lines)
        with self._state_lock:
            self._state["configuration"].update(params)
            if xlr:
                self._state["xlr"] = xlr
            self._state["updated_at"] = time.time()

    def _merge_slot(self, slot_id, params):
        """Fusionne (et n'écrase pas) : une notification `502` peut ne porter qu'une
        partie des champs, et remplacer tout le slot ferait clignoter les valeurs
        absentes du message."""
        key = str(slot_id)
        with self._state_lock:
            cur = dict(self._state["slots"].get(key) or {})
            cur.update(params)
            cur["slot id"] = key
            self._state["slots"][key] = cur
            self._state["updated_at"] = time.time()

    # --------------------------------------------------------------- lecture

    def _read_loop(self, sock):
        """Découpe le flux en blocs et les trie. Meurt avec la socket."""
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._feed(raw.rstrip(b"\r").decode("utf-8", "replace"))
        # Réveille une commande en attente plutôt que de la laisser expirer sur son délai.
        self._resp.put(Response(0, "connexion fermée", []))

    def _feed(self, line):
        if self._block is None:
            m = _HEADER.match(line)
            if not m:
                return                                  # ligne parasite hors bloc : ignorée
            code, text = int(m.group(1)), m.group(2).strip()
            if text.endswith(":"):
                self._block = (code, text[:-1].strip(), [])
            else:
                self._emit(Response(code, text, []))
            return
        code, text, lines = self._block
        if line.strip() == "":
            self._block = None
            self._emit(Response(code, text, lines))
        else:
            lines.append(line.strip())

    def _emit(self, resp):
        if 500 <= resp.code < 600:
            self._on_async(resp)
        else:
            self._resp.put(resp)

    def _on_async(self, resp):
        params = resp.params
        self._log_event(resp.code, resp.text, params)
        topic = ASYNC_MAP.get(resp.code)
        if topic == "connection":
            self._touch(model=params.get("model"),
                        protocol_version=params.get("protocol version"))
            self._preamble.set()
        elif topic == "slot":
            self._merge_slot(params.get("slot id") or "1", params)
        elif topic == "configuration":
            self._set_configuration(resp.lines, params)
        elif topic in ("transport", "remote"):
            with self._state_lock:
                self._state[topic].update(params)
                self._state["updated_at"] = time.time()
        else:
            # Non cartographié : conservé tel quel, code compris. L'UI l'affiche brut —
            # c'est la seule façon honnête de rendre une information qu'on ne sait pas nommer.
            with self._state_lock:
                self._state["extra"][str(resp.code)] = {
                    "text": resp.text, "params": params, "at": time.time()}
                self._state["updated_at"] = time.time()
        if self._on_event:
            try:
                self._on_event(self, resp)
            except Exception:                           # noqa: BLE001
                pass

    # -------------------------------------------------------------- écriture

    def _write(self, payload):
        sock = self._sock
        if not sock:
            raise HyperDeckError("non connecté")
        try:
            sock.sendall(payload.encode("utf-8"))
        except OSError as e:
            raise HyperDeckError("écriture impossible : %s" % e)

    def _exec(self, command, timeout):
        while not self._resp.empty():       # réponse orpheline d'une commande expirée
            self._resp.get_nowait()
        self._write(command if command.endswith("\r\n") else command + "\r\n")
        try:
            resp = self._resp.get(timeout=timeout)
        except queue.Empty:
            raise HyperDeckError("pas de réponse à « %s »" % command.splitlines()[0])
        if resp.code == 0:
            raise HyperDeckError(resp.text)
        return resp

    def command(self, command, timeout=8.0):
        """Envoie une commande et rend son bloc de réponse.

        Ne lève QUE sur un échec de dialogue. Un refus de l'appareil (« 111 remote control
        disabled », « 102 invalid value ») est une réponse valide : elle remonte avec son
        code, à charge de l'appelant de la présenter. Confondre les deux ferait passer un
        réglage refusé pour une panne réseau.
        """
        with self._cmd_lock:
            resp = self._exec(command, timeout)
            if resp.code == 122 and (self.user or self.password):
                # Mode sécurisé : on s'authentifie et on rejoue la commande une fois.
                auth = "authenticate:\r\nusername: %s\r\npassword: %s\r\n\r\n" % (
                    self.user, self.password)
                if self._exec(auth, timeout).ok:
                    resp = self._exec(command, timeout)
            return resp

    def command_or_raise(self, command, timeout=8.0):
        """Variante qui transforme un refus en exception — pour les appels internes dont
        l'échec n'a pas de sens à afficher ligne à ligne."""
        resp = self.command(command, timeout)
        if not resp.ok:
            raise HyperDeckError("%d %s" % (resp.code, resp.text))
        return resp


def esc_value(v):
    """Valeur de paramètre nettoyée : le protocole étant orienté ligne, un saut de ligne
    dans un nom de clip couperait la commande en deux et le reste serait interprété comme
    une commande à part entière."""
    return re.sub(r"[\r\n]+", " ", "" if v is None else str(v)).strip()
