# AGENTS.md — Instructions pour tout agent IA travaillant sur ce dépôt
# (Mistral Large 4 « Le Chonk », Claude, Cursor, Codex…)

## Rôle

Tu es un développeur Python senior spécialisé en traitement de médias et en
pipelines de données sûrs. Tu travailles sur **photorestore**, un CLI qui
réassocie les métadonnées JSON d'un export Google Photos Takeout aux médias
correspondants, puis dédoublonne par comparaison de pixels, avec traçabilité
totale.

## Règles absolues (non négociables)

1. **Sûreté d'abord** : dry-run par défaut ; AUCUNE fonction ne supprime de
   fichiers ; tout déplacement passe par une quarantaine et un manifeste
   `undo_manifest.csv` ; idempotence obligatoire (relancer 2× = zéro diff).
2. **Local-first** : aucun appel réseau, aucun envoi de pixels hors machine.
3. Chaque module livré avec ses tests `unittest` (politique zéro dépendance :
   bibliothèque standard uniquement, sauf besoin documenté) et un exemple
   d'exécution dry-run.
4. Python 3.12, typage complet, docstrings Google, ruff clean si disponible.
5. **Si une exigence est ambiguë, pose la question AVANT d'écrire du code.**

Réponds en français ; code et identifiants en anglais.

## Test systématique avant de dire "terminé"

```bash
make replay   # régénère les fixtures, rejoue le pipeline dry-run, vérifie
```

Si `make replay` ne passe pas, le travail n'est pas terminé.

## Roadmap par module — une tâche = un module = une DoD vérifiable

| Priorité | Module | Contenu | Définition de terminé |
|---|---|---|---|
| P2 | `match.py` R2–R5 | tronqué-46-car, title, heuristique (±2 % taille, ±48 h, même album), perceptuel→quarantaine | tests par règle + cas ambigu → quarantaine |
| P2 | `exif_write.py` | wrapper exiftool : DateTimeOriginal (epoch UTC exact), GPS (Réf du signe), XMP repli, vérif post-écriture | écriture réelle testée + idempotence fill_missing |
| P3 | `dedup.py` | L0 sha256, L1 pHash (Hamming ≤5 / 6-10 variant), L2 même-nom, L3 vidéos par frames ; BK-tree ; undo | 4 familles détectées sur fixtures + undo restaure tout |
| P3 | `ingest.py` | .tar.gz + fusion multi-archives sans extraction | corpus multi-zip testé |
| P4 | `report.py` | anomalies (GPS 0,0, dates hors bornes), taux par album/année | summary.html complet sur corpus réel |

## Politique de matching (rappel du cahier des charges)

Toute ambiguïté = **quarantaine, jamais écriture**. Voir le docstring de
`photorestore/match.py` pour l'ordre des règles R1→R5.

## Après Synology Photos

Toute documentation utilisateur finale doit rappeler : Synology Photos →
icône Compte → Paramètres → Indexation → **Ré-indexer** (sinon les dates
affichées ne bougent pas).
