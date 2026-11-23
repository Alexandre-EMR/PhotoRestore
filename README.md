# PhotoRestore

> Réassocie les métadonnées JSON de ton export **Google Photos Takeout** à tes
> photos et vidéos, prépare le **dédoublonnage par pixels**, journalise chaque
> décision, et t'accompagne jusqu'à la **ré-indexation Synology Photos**.
> Local-first : tes fichiers ne quittent jamais ta machine.

## Pourquoi

Google Takeout livre les photos d'un côté et les métadonnées (date de prise de
vue, GPS, description) dans des fichiers JSON séparés — avec des pièges :
noms de sidecars tronqués à 46 caractères, doublons `(1)`, copies `-edited`,
photo et JSON éclatés dans des ZIP différents. Une fois les photos importées
dans Synology Photos, les dates affichées sont fausses si l'EXIF n'a pas été
restauré avant l'import.

PhotoRestore industrialise cette restauration avec une règle d'or :
**toute ambiguïté est mise en quarantaine, jamais écrite sur le mauvais fichier.**

## Invariants

1. **Dry-run par défaut** — rien n'est écrit sans `--apply` (confirmé).
2. **Idempotent** — relancer deux fois ne change rien.
3. **Aucune suppression** — les doublons partent en quarantaine avec manifeste
   d'annulation (`photorestore undo`).
4. **Zéro dépendance** — bibliothèque standard Python 3.12 uniquement.
   Exception documentée : l'extra optionnel `perceptual` (Pillow + ImageHash)
   active la règle R5 (pHash) ; sans lui, R5 est proprement ignorée.
5. **Local-first** — aucun appel réseau, aucun pixel envoyé ailleurs.

## Installation

```bash
git clone https://github.com/Alexandre-EMR/photorestore.git
cd photorestore
python3 -m photorestore.cli --help

# Optionnel — active la règle R5 (propositions perceptuelles en quarantaine)
pip install .[perceptual]
```

## Utilisation

```bash
# 1. Inventorier un dossier Takeout déjà extrait (rien n'est modifié)
python3 -m photorestore.cli ingest "/chemin/Takeout/Google Photos"

# ... ou depuis les archives ZIP (extraction dans un workdir)
python3 -m photorestore.cli --zip-mode --workdir ./takeout ingest ./zips

# 2. Cascade de matching + rapports (dry-run, aucune écriture)
python3 -m photorestore.cli match

# 3. Relire les rapports
#    reports/match_report.csv   — chaque décision, la règle utilisée
#    reports/quarantine_report.csv — items ambiguïs avec la raison
#    reports/summary.html      — tableau de bord
#    reports/audit_export.jsonl — export texte pour audit par agent IA

# 4. (P2) écriture EXIF réelle, après relecture
python3 -m photorestore.cli --apply fix
```

## État de la roadmap

| Phase | Statut | Contenu |
|---|---|---|
| P1 squelette | ✅ | ingest, manifeste SQLite resumable, R1 exact, rapports, CLI, corpus de test |
| P2 cœur (match) | ✅ | cascade R1–R5 complète : tronqué-46, title, heuristique, perceptuel→quarantaine |
| P2 cœur (fix) | ✅ | écriture EXIF/XMP via exiftool : fill_missing/json_wins/ask, offset UTC exact, GPS signé, repli XMP, renommage extensions mensongères, vérification post-écriture |
| P3 dedup | ⬜ | L0 sha256 · L1 pHash · L2 même-nom · L3 vidéos · undo |
| P4 consolidation | ⬜ | anomalies, Docker Synology (Container Manager) |

La commande `dedup` existe déjà mais rend un **code retour 2** documenté :
elle refuse poliment tant que le module M5 (P3) n'est pas implémenté.
`fix` est implémenté (M4) et reste en dry-run sans `--apply`.

## Développement assisté par IA

Ce dépôt est conçu pour le développement avec un agent de code
(Mistral Large 4 « Le Chonk » dans Mistral Studio, ou tout autre agent).
Les règles pour l'agent sont dans **AGENTS.md** : invariants, roadmap par
module avec définition de « terminé », et le contrat `make replay`.

```bash
make test    # suite unittest
make replay  # régénère le corpus synthétique + rejoue le pipeline dry-run
```

## Référence exiftool (pendant que M4 arrive)

```bash
exiftool -r -d %s \
  -tagsfromfile "%d/%F.supplemental-metadata.json" \
  "-DateTimeOriginal<PhotoTakenTimeTimestamp" \
  "-GPSLatitude<GeoDataLatitude" \
  "-GPSLongitude<GeoDataLongitude" \
  -overwrite_original_in_place "Takeout/Google Photos"
```

⚠️ Après toute écriture EXIF sur le NAS : **Synology Photos → icône Compte →
Paramètres → Indexation → Ré-indexer**. Sans cette étape, les dates affichées
ne bougent pas.

## Licence

Apache-2.0 — voir [LICENSE](LICENSE).
