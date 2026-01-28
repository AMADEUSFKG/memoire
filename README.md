# memoire

## Objectif
Ce dépôt contient un outil local pour indexer vos sources PDF et les interroger comme un moteur de recherche orienté analyse stratégique. L'index fournit :

- classement automatique des documents par chapitre,
- suggestion d'angles d'analyse (théorique, géopolitique, industriel, stratégique),
- signalement des sources centrales vs secondaires,
- recherche plein texte avec extraits.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Utilisation rapide

### 1) Initialiser la base

```bash
python tools/index_memoire.py init
```

### 2) Indexer vos PDF

```bash
python tools/index_memoire.py index ./
```

### 3) Interroger (recherche FTS)

```bash
python tools/index_memoire.py query "autonomie strategique" --limit 5
```

### 4) Voir le classement suggéré

```bash
python tools/index_memoire.py report
```

## Ajuster la logique de classement

Modifiez `config/classification.yaml` pour adapter les mots-clés par chapitre ou par angle d'analyse. Le système utilise des scores simples basés sur la présence de mots-clés.
