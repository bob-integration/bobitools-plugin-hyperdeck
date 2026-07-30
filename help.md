# HyperDeck

Pilotage d'un parc d'enregistreurs **Blackmagic HyperDeck** (Studio, Extreme, Shuttle) via
le *HyperDeck Ethernet Protocol* — protocole texte public, sur **TCP 9993**.

## Ce que l'outil fait

- **Voir** l'état de toutes les machines d'un coup d'œil : lecture / enregistrement /
  arrêt, timecode, format, disque actif et durée restante.
- **Régler** : entrées vidéo et audio, format de fichier, timecode, référence, préfixe des
  fichiers, entrées XLR… Les réglages proposés sont ceux que la MACHINE déclare.
- **Piloter** : REC / STOP / PLAY, vitesse, aller à un timecode, choix du slot.
- **Agir sur plusieurs machines à la fois** : lancer six enregistrements ensemble, tout
  arrêter, appliquer un format de fichier à tout le parc.

## Le point à connaître avant tout : le mode « remote »

Un HyperDeck refuse toute commande de transport quand son pilotage à distance est coupé —
il répond `111 remote control disabled`. C'est de très loin la première cause
d'incompréhension avec ce protocole. L'outil l'affiche en bandeau sur la machine concernée,
avec deux boutons :

- **Activer le pilotage** — change le réglage *dans la machine*, durablement ;
- **Forcer pour cette session** — override valable seulement le temps de la connexion.

L'override survit tant que l'outil reste connecté, ce qui est justement le cas : l'outil
garde une connexion permanente ouverte par machine.

## Une connexion permanente par machine

Contrairement aux outils qui interrogent un appareil à chaque clic, celui-ci **reste
connecté**. Trois raisons :

1. l'appareil pousse ses changements tout seul (`notify`) — l'état affiché est celui de la
   machine, pas celui d'un sondage vieux de dix secondes ;
2. l'override du mode remote ne survit pas à la fermeture de la connexion ;
3. un HyperDeck n'accepte qu'un petit nombre de clients simultanés (`120 connection
   failed`) — mieux vaut en consommer un proprement que d'en ouvrir un par requête.

Conséquence utile : les vues de parc lisent un cache local, **sans aucune I/O réseau**.
Elles peuvent donc se rafraîchir toutes les deux secondes sans peser sur quoi que ce soit.
Un filet de sécurité relit tout de même l'état complet à intervalle réglable, au cas où une
notification se perdrait.

Une machine injoignable est reconnectée toute seule, avec un recul croissant jusqu'à 30 s.

## Ce que l'outil ne présuppose pas

Les modèles diffèrent beaucoup : un Shuttle n'a pas d'entrée XLR, un Extreme 8K en a
quatre ; les formats vidéo et de fichier dépendent du firmware. L'outil ne code donc
**aucune** liste de réglages en dur :

- l'onglet **Réglages** est construit à partir de la réponse `configuration` de la machine.
  Une clé ajoutée par un firmware plus récent apparaît en texte libre plutôt que d'être
  ignorée ;
- les valeurs proposées dans les listes déroulantes sont des **suggestions** issues de la
  documentation. C'est l'appareil qui arbitre : son refus (`102 invalid value`,
  `103 unsupported`) s'affiche tel quel ;
- la valeur courante est toujours présente dans la liste, même si la documentation ne la
  connaît pas.

## Refus ≠ panne

Une réponse `1xx` de la machine (`111 remote control disabled`, `105 no disk`,
`102 invalid value`) décrit l'état de l'appareil : elle est affichée telle quelle, code
compris. Seule l'impossibilité de dialoguer (machine éteinte, port fermé) est présentée
comme une erreur de l'outil.

## Les onglets du détail

| Onglet | Contenu |
| --- | --- |
| **Transport** | REC / STOP / PLAY, vitesse, aller à un TC, slots, identify, redémarrage |
| **Réglages** | tout ce que la machine expose, par rubrique, entrées XLR comprises |
| **Médias** | contenu d'un disque (`disk list`) ou de la timeline (`clips get`) |
| **Réseau** | signets, partage sélectionné, volume monté, découverte mDNS |
| **Événements** | journal des notifications reçues + messages `5xx` non interprétés, bruts |
| **Console** | commande brute, complétée par la liste `commands` du firmware |

L'onglet **Événements** montre aussi les messages asynchrones que l'outil ne sait pas
nommer (images perdues, position timeline… selon le firmware). Ils sont affichés bruts
plutôt que jetés : l'appareil les envoie, ils veulent dire quelque chose.

## Actions groupées

Sélectionnez plusieurs machines (cases à gauche), la barre d'actions apparaît en bas.
Les commandes partent **en parallèle**, et le compte-rendu est **ligne à ligne** : une
action groupée réussit presque toujours partiellement, et annoncer « OK » alors que deux
machines sur six n'ont pas démarré serait le pire service à rendre en direct.

## Volumes réseau

Un HyperDeck ne sait mémoriser un partage que **pour lui-même**. Sans outil, il faut donc
retaper serveur, identifiant et chemin sur chaque machine. L'onglet **Volumes réseau**
renverse ça : on saisit un chemin **une fois** dans la bibliothèque de l'outil (nom, adresse
du partage, identifiant, mot de passe), puis on l'applique où l'on veut.

Deux façons d'appliquer :

- **par lot** — on coche des machines, on choisit le chemin et l'action ;
- **par clic** — dans la matrice, une cellule pose le chemin sur cette machine-là (ou
  démonte si le volume y est monté).

### Signet ≠ sélectionné ≠ monté

C'est le point important de cette rubrique, et le protocole y invite à l'erreur. Trois
faits distincts, affichés distinctement :

| État | Ce que ça veut dire |
| --- | --- |
| **signet** | l'adresse est mémorisée dans la machine. Rien n'est monté. |
| **sélectionné** | le montage a été **demandé**. `nas select` est **asynchrone** : si le serveur ne répond pas, ça reste dans cet état indéfiniment. |
| **monté** | le slot réseau porte cette adresse et est monté — le volume est **réellement** utilisable pour enregistrer et relire. |

Autrement dit : **un `200 ok` ne prouve pas qu'un volume est monté.** Il dit que la demande
a été acceptée. Seule la ligne « monté » de la matrice — qui vient de
`slot info: device: network` — le prouve. Le compte-rendu d'une application groupée le
rappelle explicitement, et la matrice est relue juste après.

Un seul partage peut être monté à la fois sur une machine : en monter un autre fait
retomber le précédent au rang de signet.

### Comparaison d'adresses

Pour savoir si un chemin de la bibliothèque est celui que la machine a, on ne compare que
ce qui est sûr : espaces, casse du schéma et de l'hôte, barre oblique finale. **Aucune
équivalence nom ↔ IP** : déclarer identiques `smb://nas.local/X` et `smb://10.0.0.5/X`
serait une supposition, et la matrice afficherait « monté » là où rien ne l'est.

### Format des réponses

Le protocole documente la **syntaxe d'écriture** des commandes `nas` mais pas le format des
réponses de lecture. L'outil n'invente donc aucune structure : il extrait l'URL (seule
chose dont on soit certain) et affiche la **ligne d'origine** à côté. L'onglet Réseau d'une
machine montre les deux.

Retirer un chemin de la bibliothèque de l'outil **ne touche pas aux machines** : les signets
déjà posés y restent. Les enlever d'autorité démonterait un volume en cours d'usage.

## Entrées XLR

`xlr input id` et `xlr type` sont les seuls paramètres du protocole à être **répétés** dans
la réponse — une paire par entrée. L'outil les lit positionnellement et affiche une liste
par entrée ; l'écriture renvoie bien les deux dans la *même* commande, sans quoi la machine
ne sait pas de quelle entrée on parle.

## Vérifier l'interface avant de livrer

`node mount_check.js .` monte l'UI contre un DOM minimal construit à partir des
identifiants réellement présents dans `page.html`, puis **rejoue tous les gestionnaires de
clic et de saisie**. Ça n'attrape pas un défaut d'apparence, mais ça attrape ce que
`node --check` laisse passer : une fonction supprimée par une réécriture, un identifiant
absent du HTML, un gestionnaire qui lève. À passer après toute modification de `page.js` ou
`page.html`.

## Mode sécurisé

Si la machine est en mode sécurisé, renseignez identifiant et mot de passe dans sa fiche.
L'authentification est transparente : à la première réponse `122 authentication required`,
l'outil s'authentifie puis rejoue la commande.

## Ce qui n'est pas (encore) là

- **formatage des supports** (`format: prepare` / `format: confirm`) — destructif, laissé
  volontairement de côté pour une première version ;
- **édition de la timeline** (`clips add` / `clips remove` / `playrange`) — la timeline est
  listée, pas modifiée ; là aussi la Console couvre le besoin ponctuel ;
- **jog / shuttle** au geste — les commandes sont routées côté serveur, mais l'UI n'a pas
  de molette.
