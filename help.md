# HyperDeck

Pilotage d'un parc d'enregistreurs **Blackmagic HyperDeck** (Studio, Extreme, Shuttle) via
le *HyperDeck Ethernet Protocol* — protocole texte public, sur **TCP 9993**.

## Ce que l'outil fait

- **Voir** l'état de toutes les machines d'un coup d'œil : lecture / enregistrement /
  arrêt, timecode, format, disque actif et durée restante.
- **Régler** : entrées vidéo et audio, format de fichier, timecode, référence, préfixe des
  fichiers, entrées XLR… Les réglages proposés sont ceux que la MACHINE déclare.
- **Piloter** : REC / STOP / PLAY, vitesse, aller à un timecode, choix du support.
- **Agir sur plusieurs machines à la fois** : lancer six enregistrements ensemble, tout
  arrêter, appliquer un format de fichier à tout le parc.
- **Formater un support à distance**, en deux temps (cf. « Supports et formatage »).
- **Se laisser piloter par un contrôleur broadcast** en Ember+ (cf. « L'arbre Ember+ »).

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

## Agir sur plusieurs machines

Cochez deux machines ou plus : **le panneau de détail devient celui de la sélection**. Ce
sont les mêmes onglets et les mêmes champs que pour une machine seule — on ne réapprend
pas une interface pour piloter un parc. Il n'y a donc **pas de barre d'actions groupées** :
tout se fait là où l'on regarde déjà.

- **Transport** — REC, Stop et Lecture portent le nombre de machines dans leur libellé
  (« ⏺ Enregistrer · 6 machines »), et un tableau donne l'état de chacune.
- **Réglages** — le formulaire est *fusionné* (voir ci-dessous).
- **Horloge** — un serveur de temps à appliquer à tout le lot, et l'état de chaque machine.

### Réglages fusionnés : commun ou divergent

Chaque champ dit ce qu'il en est :

| Cas | Affichage |
|---|---|
| Toutes les machines ont la même valeur | la valeur, comme d'habitude |
| Les valeurs diffèrent | **« — valeurs différentes — »**, plus un repère `⚠ n valeurs` |
| Le réglage n'existe pas partout | un compteur `2/5` (modèles différents) |

Le repère `⚠ n valeurs` donne le détail **au survol** et **au clic** : quelle valeur, sur
quelles machines nommément. On sait donc exactement ce qu'on s'apprête à écraser.

> **Seuls les champs que vous touchez sont écrits.** Ouvrir l'écran et valider n'aligne
> rien : les champs auxquels vous n'avez pas touché restent tels quels sur chaque machine.
> Sans cette règle, consulter les réglages d'un parc suffirait à l'uniformiser par
> accident sur la première machine venue.

Un champ modifié se distingue visuellement, le bouton d'application compte les
modifications en attente, et une confirmation récapitule ce qui va partir et sur combien de
machines. Pour renoncer à une modification, ramenez le champ sur « — valeurs différentes — ».

Une machine dont les réglages n'ont pas encore été lus (déconnectée, par exemple) est
**nommée** en tête de panneau et ne compte pas dans les divergences : la traiter comme une
valeur vide ferait apparaître des écarts qui n'existent pas.

## Horloge et NTP

L'onglet **Horloge** d'une machine montre l'heure **telle que l'appareil la voit** — donc
telle qu'il **datera ses fichiers** —, l'écart avec l'heure du serveur, le fuseau réglé sur
la machine, l'état de synchronisation et le serveur de temps. Le serveur NTP se modifie
ici, machine par machine, ou d'un coup sur une sélection depuis la barre d'actions
groupées (champ *Serveur de temps*).

La vue d'ensemble porte une colonne **Horloge**, volontairement distincte du **Timecode** :
le timecode est une donnée de montage, l'horloge date les fichiers. Les confondre fait
chercher une dérive au mauvais endroit.

> **Le cas à connaître :** une machine peut afficher une heure parfaitement juste avec une
> synchronisation **en échec**. Elle a été mise à l'heure un jour et n'a pas encore dérivé
> — mais elle dérivera, et personne ne le verra avant que les fichiers ne soient mal datés.
> C'est exactement l'état de la machine sur laquelle cette fonction a été mise au point :
> heure exacte à 0,1 s près, NTP en échec depuis longtemps. D'où l'affichage systématique
> des **deux** faits, jamais l'un sans l'autre.

L'écart affiché ne dépend d'aucun fuseau : on compare deux dates absolues. Le fuseau ne
sert qu'à afficher l'heure locale de la machine. Une machine dont l'accès web est coupé
(réglage *Accès réseau* de l'appareil) est signalée **indisponible** — jamais supposée à
l'heure.

Enfin, la machine **accepte n'importe quelle adresse de serveur** sans la vérifier : un nom
qui ne résout pas est enregistré sans broncher. C'est l'état relu juste après l'écriture
qui dit si elle y arrive, et c'est lui que l'outil affiche.

### D'où vient cette fonction

Le HyperDeck Ethernet Protocol (TCP 9993) **ignore totalement l'horloge** : sur un Studio
4K Pro en firmware 9.0.2, `clock`, `time`, `date` et `ntp` répondent tous
`100 syntax error`, et aucune des **135 commandes** que la machine déclare elle-même ne
touche à l'heure. L'API REST de contrôle (`/control/api/v1`, **70 points d'entrée** décrits
par l'appareil) ne la connaît pas davantage.

L'horloge vit dans une **troisième interface**, servie sur le port 80 sous
`/admin/api/v1/` : celle de l'utilitaire *Blackmagic HyperDeck Setup*. Blackmagic ne la
documente nulle part ; `admin.py` a été écrit d'après l'observation du dialogue réel entre
Setup et une machine. Les formats sont donc **relevés, pas supposés** — et sans garantie de
stabilité d'un firmware à l'autre, d'où la tolérance systématique aux champs manquants.

Cette API ne demande **aucune authentification** alors qu'elle règle aussi le réseau, les
accès et les comptes. L'outil s'en tient délibérément à l'horloge : une écriture malheureuse
sur l'interface réseau couperait la machine du réseau.

## Supports et formatage

### Pourquoi l'outil ne parle pas en numéros de slot

Un HyperDeck désigne ses supports de deux façons, et une seule est stable. Les lecteurs de
cartes ont un numéro (1, 2). Le **slot externe** — le dernier numéro, 3 sur un Studio HD
Plus — n'est pas un support : c'est une place occupée par le disque externe *sélectionné*,
réseau **ou** USB-C. Un disque externe présent mais non sélectionné n'a alors aucun numéro :
il se présente `slot id: none`, et seul son `device` l'adresse — un jeton fabriqué par la
machine, `usb512`, que `device: usb` ne remplace pas (relevé : carte et SSD montés,
`slot info: device: usb` répond quand même `105 no disk`).

Autrement dit : le numéro d'un support change selon ce qui est sélectionné, et un support
peut n'en avoir aucun. L'outil présente donc des **positions** — Carte SD 1, Carte SD 2,
SSD USB-C, Réseau — qu'il traduit lui-même en `slot id` ou en `device`. Une position est
affichée même quand le support est absent : un lecteur vide est une information.

⚠ **`usb512` désigne le PORT, pas le disque.** Il survit au débranchement, au rebranchement
et au changement de SSD. Il adresse de façon fiable, mais ne dit jamais *quel* disque est en
place — deux disques d'un même lot sont indiscernables au protocole.

⚠ **Les disques externes s'excluent.** Rendre le SSD actif **désélectionne** le volume
réseau, et inversement. L'outil le dit à chaque bascule plutôt que de laisser le découvrir
en enregistrant au mauvais endroit.

### Le formatage, en deux temps

`format: prepare:` arme et ne détruit rien ; il rend un jeton. `format: confirm: <jeton>`
exécute, en 4 à 6 secondes sur un SSD d'un téraoctet. Deux systèmes de fichiers seulement,
et la machine le fait respecter : **`exFAT` et `HFS+`** — tout le reste reçoit un
`160 invalid format`. Un nom de volume peut être donné (`name:`) ; sans lui, le volume garde
le sien. Ce nom **ne peut pas contenir d'espace** : le protocole lirait le mot suivant comme
un nouveau paramètre, et l'outil refuse donc en amont.

**Le point à connaître : la machine ne fait pas périmer son jeton.** Un jeton émis quinze
minutes plus tôt a été accepté et le disque formaté ; aucune commande ne permet d'annuler
une préparation. La péremption d'une minute tenue par l'outil est donc la **seule** barrière
qui existe — « Annuler » n'annule que côté outil, jamais côté machine.

Le volume **réseau** n'est pas formatable depuis cet outil, bien que la commande l'accepte :
c'est un partage commun à tout un parc, et l'effacer par mégarde depuis un contrôleur n'a
pas le même prix qu'une carte.

Refus possibles, tous distincts : `160 invalid format` (système inconnu), `105 no disk`
(support absent), `161 invalid token` (jeton jamais émis), `150 invalid state` (jeton déjà
consommé — ils sont à usage unique).

## L'arbre Ember+

L'outil publie son parc au service Ember+ en **mode libre**. Une machine est un nœud,
**identifié par son nom d'inventaire** : devant un contrôleur broadcast, « Hyperdeck 3 »
parle et « 10.1.12.3 » non.

⚠ **Le contrôleur lie son câblage à l'IDENTIFIANT, pas au numéro de chemin** (mesuré sur le
VSM). Renommer une machine lui fait donc perdre son câblage, alors que la renuméroter est
transparent. **Figer les noms avant de câbler.** Deux machines de même nom donnent deux
nœuds indistinguables : l'outil le signale au journal sans renommer d'office, un suffixe
automatique casserait le câblage de celle qui était déjà là.

Sous chaque machine : l'état et le transport en paramètres directs, puis deux nœuds —
**Destination** (support actif, état, volume, temps restant, et une position par support) et
**Formatage** (système de fichiers, support, Préparer, Prêt à confirmer, Confirmer, dernier
résultat).

Le formatage y est un geste **en deux temps**, jamais un bouton : « Confirmer » n'agit que
si « Prêt à confirmer » est vrai, et le support choisi retombe à « — » après chaque
confirmation. Les deux impulsions n'agissent que sur une valeur *vraie* — un contrôleur
réémet ses valeurs à la reconnexion et au rappel d'un instantané, et un booléen relu ne doit
rien déclencher.

Les **énumérations sont des contrats** : c'est l'index qui voyage. Les formats de fichier
viennent de la liste figée de l'outil et non de ce que la machine annonce ; quand elle
annonce un format hors liste, le paramètre retombe sur « — » et la valeur réelle reste
lisible dans « Format de fichier (annoncé) ».

## Ce qui n'est pas (encore) là

- **édition de la timeline** (`clips add` / `clips remove` / `playrange`) — la timeline est
  listée, pas modifiée ; là aussi la Console couvre le besoin ponctuel ;
- **jog / shuttle** au geste — les commandes sont routées côté serveur, mais l'UI n'a pas
  de molette.
