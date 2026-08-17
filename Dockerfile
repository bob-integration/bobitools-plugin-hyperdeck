# SPDX-License-Identifier: GPL-3.0-or-later
# Image autonome de l'outil « HyperDeck ». Le HyperDeck Ethernet Protocol est un protocole
# texte sur TCP : la bibliothèque standard suffit, AUCUNE dépendance à installer. Une image
# sans pip install, c'est aussi une image qui se reconstruit en deux secondes hors ligne.
FROM python:3.13-slim

WORKDIR /app
COPY server.py fleet.py protocol.py schema.py admin.py supports.py ember.py /app/

# Parc (adresses, identifiants) persisté dans un volume monté par l'app sur /data
# (cf. docker.volume du plugin.json). L'ÉTAT des machines, lui, n'est jamais stocké : il
# vit dans les appareils, et le dupliquer sur disque créerait une seconde vérité.
VOLUME ["/data"]

# Port HTTP interne — DOIT correspondre à docker.port du plugin.json. L'app publie ce port
# sur 127.0.0.1:<aléatoire> et proxifie /api/tools/hyperdeck/* vers lui.
EXPOSE 8080

CMD ["python", "-u", "/app/server.py"]
