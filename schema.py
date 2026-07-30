# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Description des réglages `configuration` du HyperDeck : libellé, type, valeurs proposées.

## Ce que ce fichier est, et ce qu'il n'est pas

Il ne DÉCIDE pas des réglages affichés. C'est la réponse `211 configuration:` de l'appareil
qui fait foi : `describe()` ne décrit que les clés réellement renvoyées par la machine en
face, et une clé inconnue de cette table est présentée en texte libre plutôt que masquée.
Un HyperDeck Shuttle n'a pas d'entrée XLR, un Extreme 8K en a quatre : afficher un réglage
que l'appareil n'a pas, ou cacher un réglage qu'un firmware récent a ajouté, seraient deux
façons de mentir.

## Les listes de valeurs sont des SUGGESTIONS

Les formats vidéo et de fichier dépendent du modèle, du firmware et parfois du format
courant, et le protocole n'offre aucun moyen de demander « que sais-tu accepter ? ». Les
listes ci-dessous sont donc celles de la documentation, proposées telles quelles ; c'est
l'appareil qui arbitre, et son refus (« 102 invalid value », « 103 unsupported ») remonte
tel quel à l'écran. La valeur courante est toujours ajoutée à la liste si elle en est
absente — sans quoi ouvrir la liste déroulante afficherait autre chose que ce que la
machine fait réellement.
"""

# Ordre d'affichage des rubriques.
SECTIONS = [
    ("inputs", "Entrées"),
    ("record", "Enregistrement"),
    ("timecode", "Timecode"),
    ("audio", "Audio"),
    ("reference", "Référence et sortie"),
    ("other", "Divers"),
]

VIDEO_INPUTS = ["SDI", "4xSDI", "HDMI", "component", "composite"]
AUDIO_INPUTS = ["embedded", "XLR", "RCA"]
AUDIO_CODECS = ["PCM", "AAC"]
TIMECODE_INPUTS = ["external", "embedded", "internal", "preset", "clip"]
TIMECODE_OUTPUTS = ["clip", "timeline"]
TIMECODE_PREFS = ["default", "dropframe", "nondropframe"]
RECORD_TRIGGERS = ["none", "recordbit", "timecoderun"]
REFERENCE_SOURCES = ["auto", "input", "external"]
XLR_TYPES = ["line", "mic"]

FILE_FORMATS = [
    "H.264High_SDI", "H.264High", "H.264Medium", "H.264Low", "H.264High10_422",
    "H.265High_SDI", "H.265High_422", "H.265High", "H.265Medium", "H.265Low",
    "QuickTimeProResHQ", "QuickTimeProRes", "QuickTimeProResLT", "QuickTimeProResProxy",
    "QuickTimeDNxHR_HQX", "DNxHR_HQX", "QuickTimeDNxHR_SQ", "DNxHR_SQ",
    "QuickTimeDNxHR_LB", "DNxHR_LB",
    "QuickTimeDNxHD220x", "DNxHD220x", "QuickTimeDNxHD145", "DNxHD145",
    "QuickTimeDNxHD45", "DNxHD45",
]

VIDEO_FORMATS = [
    "NTSC", "PAL", "NTSCp", "PALp",
    "720p50", "720p5994", "720p60",
    "1080p23976", "1080p24", "1080p25", "1080p2997", "1080p30", "1080p50", "1080p5994",
    "1080p60", "1080i50", "1080i5994", "1080i60",
    "2160p23.98", "2160p24", "2160p25", "2160p29.97", "2160p30", "2160p50", "2160p59.94",
    "2160p60",
    "4Kp23976", "4Kp24", "4Kp25", "4Kp2997", "4Kp30", "4Kp50", "4Kp5994", "4Kp60",
    "4320p23.98", "4320p24", "4320p25", "4320p29.97", "4320p30", "4320p50", "4320p59.94",
    "4320p60", "8Kp23976", "8Kp24", "8Kp25",
]

# clé protocolaire → description. `order` sert au tri À L'INTÉRIEUR d'une rubrique.
PARAMS = {
    "video input": {"label": "Entrée vidéo", "type": "enum", "choices": VIDEO_INPUTS,
                    "section": "inputs", "order": 10},
    "audio input": {"label": "Entrée audio", "type": "enum", "choices": AUDIO_INPUTS,
                    "section": "inputs", "order": 20,
                    "note": "supplantée par « mappage XLR / RCA » sur les modèles récents"},
    "file format": {"label": "Format de fichier", "type": "enum", "choices": FILE_FORMATS,
                    "section": "record", "order": 10,
                    "note": "un changement peut redémarrer l'appareil (« 213 deck rebooting »)"},
    "audio codec": {"label": "Codec audio", "type": "enum", "choices": AUDIO_CODECS,
                    "section": "audio", "order": 10},
    "audio input channels": {"label": "Canaux audio enregistrés", "type": "number",
                             "section": "audio", "order": 20},
    "audio mapping": {"label": "Mappage audio", "type": "text", "section": "audio", "order": 30},
    "xlr mapping": {"label": "Mappage XLR (premier canal, ou « none »)", "type": "text",
                    "section": "audio", "order": 40},
    "rca mapping": {"label": "Mappage RCA (premier canal, ou « none »)", "type": "text",
                    "section": "audio", "order": 50},
    "timecode input": {"label": "Source du timecode", "type": "enum", "choices": TIMECODE_INPUTS,
                       "section": "timecode", "order": 10},
    "timecode output": {"label": "Timecode en sortie", "type": "enum", "choices": TIMECODE_OUTPUTS,
                        "section": "timecode", "order": 20},
    "timecode preference": {"label": "Drop frame", "type": "enum", "choices": TIMECODE_PREFS,
                            "section": "timecode", "order": 30},
    "timecode preset": {"label": "Timecode préréglé", "type": "text", "section": "timecode",
                        "order": 40, "placeholder": "HH:MM:SS:FF"},
    "record trigger": {"label": "Déclencheur d'enregistrement", "type": "enum",
                       "choices": RECORD_TRIGGERS, "section": "record", "order": 20},
    "record prefix": {"label": "Préfixe des fichiers", "type": "text", "section": "record",
                      "order": 30},
    "append timestamp": {"label": "Ajouter l'horodatage au nom de fichier", "type": "bool",
                         "section": "record", "order": 40},
    "record cache": {"label": "Cache d'enregistrement", "type": "bool", "section": "record",
                     "order": 50, "note": "sans effet si l'appareil n'a pas de cache"},
    "usb spill": {"label": "Débordement entre disques USB", "type": "bool", "section": "record",
                  "order": 60},
    "reference source": {"label": "Source de référence", "type": "enum",
                         "choices": REFERENCE_SOURCES, "section": "reference", "order": 10},
    "genlock input resync": {"label": "Resynchronisation genlock", "type": "bool",
                             "section": "reference", "order": 20},
    "default standard": {"label": "Format vidéo de lecture par défaut", "type": "enum",
                         "choices": VIDEO_FORMATS, "section": "reference", "order": 30},
}


def describe(configuration):
    """Réglages à afficher, construits à partir de ce que l'APPAREIL a renvoyé.

    Les clés répétées `xlr input id` / `xlr type` sont écartées : elles vont par paires et
    ont leur propre contrôle (cf. `protocol.xlr_pairs`)."""
    out = []
    for key, value in (configuration or {}).items():
        if key in ("xlr input id", "xlr type"):
            continue
        d = PARAMS.get(key)
        if d:
            choices = list(d.get("choices") or [])
            if choices and value not in choices:
                choices.insert(0, value)        # jamais perdre la valeur courante de vue
            out.append({"key": key, "label": d["label"], "type": d["type"],
                        "choices": choices, "section": d["section"], "order": d["order"],
                        "note": d.get("note"), "placeholder": d.get("placeholder"),
                        "value": value})
        else:
            # Clé ajoutée par un firmware plus récent que cette table : présentée en texte
            # libre, avec son nom protocolaire. Mieux vaut un contrôle brut qu'un réglage
            # invisible.
            out.append({"key": key, "label": key, "type": "text", "choices": [],
                        "section": "other", "order": 100, "note": None,
                        "placeholder": None, "value": value, "unknown": True})
    out.sort(key=lambda p: (
        [s[0] for s in SECTIONS].index(p["section"]) if p["section"] in [s[0] for s in SECTIONS] else 99,
        p["order"], p["label"]))
    return out


def sections():
    return [{"key": k, "label": lab} for k, lab in SECTIONS]
