# HyperDeck — plugin Bobi.Tools

Pilotage d'un parc d'enregistreurs **Blackmagic HyperDeck** (Studio, Extreme, Shuttle) pour
[Bobi.Tools](https://github.com/bob-integration/bobitools), par le *HyperDeck Ethernet
Protocol* (TCP 9993). L'outil garde une connexion permanente par machine : l'état affiché vient
des notifications de l'appareil, pas d'un sondage.

## Ce que fait l'outil

- **Voir** tout le parc d'un coup d'œil : transport, timecode, format, support actif, durée
  restante, horloge.
- **Régler** chaque machine à partir de ce qu'elle déclare elle-même (entrées vidéo et audio,
  format de fichier, timecode, référence, préfixe, entrées XLR), sans liste codée en dur.
- **Piloter** : REC / STOP / PLAY, vitesse, aller à un timecode, choix du support. Cocher
  plusieurs machines transforme le panneau de détail en panneau de la sélection ; seuls les
  champs modifiés sont écrits.
- **Volumes réseau** : une bibliothèque de chemins tenue par l'outil, appliquée à une ou
  plusieurs machines, avec une matrice qui distingue signet, partage sélectionné et volume
  réellement monté.
- **Horloge et NTP** : heure de chaque machine, écart avec le serveur, état de
  synchronisation, serveur de temps réglable par machine ou sur tout le parc.
- **Formatage à distance** en deux temps (exFAT ou HFS+), et **arbre Ember+** exposant le
  parc à un contrôleur broadcast.

## À savoir

- **Mode remote** : une machine dont le pilotage à distance est coupé refuse le transport
  (`111 remote control disabled`). L'outil le signale et propose de l'activer durablement ou
  de le forcer pour la session.
- Les supports sont présentés par **position** (Carte SD 1, Carte SD 2, SSD USB-C, Réseau) et
  non par numéro de slot, qui change selon le disque externe sélectionné. Sélectionner le
  SSD désélectionne le volume réseau, et inversement.
- **La machine ne fait pas périmer son jeton de formatage** : la péremption d'une minute
  tenue par l'outil est la seule barrière. Le volume réseau n'est pas formatable d'ici.
- Un `200 ok` sur `nas select` ne prouve pas qu'un volume est monté : seule la ligne
  « monté » de la matrice le prouve.
- L'horloge passe par l'API d'administration HTTP non documentée de l'appareil (celle de
  *HyperDeck Setup*), relevée sur un firmware donné : sans garantie d'un firmware à l'autre.
- Ember+ : chaque machine est un nœud identifié par son **nom**. Le contrôleur lie son
  câblage à l'identifiant : figer les noms avant de câbler.

## Prérequis

- **Bobi.Tools** avec **Docker** : l'outil tourne en conteneur (`runtime: docker`). Aucune
  dépendance Python hors bibliothèque standard.
- Des HyperDeck joignables sur **TCP 9993** (et sur le port 80 pour l'horloge).
- Pour l'arbre Ember+ : le service
  **[Ember+](https://github.com/bob-integration/bobitools-service-emberplus)** (`emberplus`).

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer ». Ou, sur une machine
neuve, en une ligne :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh) --outils hyperdeck
```

L'aide complète est dans [`help.md`](help.md), affichée dans Bobi.Tools (menu « ? » → Aide).

## Sécurité

- Le conteneur n'a pas d'authentification propre : son port n'est publié que sur `127.0.0.1`,
  il n'est donc joignable qu'à travers Bobi.Tools, qui contrôle les droits.
- Les identifiants des machines (mode sécurisé) et des partages réseau sont conservés dans le
  volume du conteneur, sans chiffrement ; ils ne sont jamais renvoyés à l'interface.
- L'API d'administration de l'appareil ne demande aucune authentification alors qu'elle règle
  aussi le réseau et les comptes : l'outil s'y limite délibérément à l'horloge.
- Les gestes sont soumis à des permissions distinctes (transport, support, formatage,
  réglages, gestion du parc), y compris lorsqu'ils viennent d'un contrôleur Ember+.

## In English

Control of a fleet of **Blackmagic HyperDeck** recorders for Bobi.Tools over the HyperDeck
Ethernet Protocol (TCP 9993), with one persistent connection per deck and state driven by
the deck's own notifications. Transport, settings built from what each deck declares,
multi-deck selection that only writes the fields you touch, a network-share library with a
bookmarked / selected / mounted matrix, clock and NTP via the deck's admin HTTP API,
two-step remote formatting, and an Ember+ tree for broadcast controllers (through the
`emberplus` service). Requires Docker.

## Licence

GPL-3.0-or-later — © 2026 BOBI SAS. Voir [LICENSE](LICENSE).
