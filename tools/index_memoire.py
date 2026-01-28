#!/usr/bin/env python3
"""Indexe des documents PDF dans une base SQLite + FTS pour recherche analytique."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover - runtime guidance
    raise SystemExit("PyYAML est requis. Installez les dépendances: pip install -r requirements.txt") from exc

CONFIG_PATH = Path("config/classification.yaml")
DEFAULT_DB = Path("memoire.sqlite")
CHUNK_TARGET = 1200
CHUNK_MIN = 400

WORD_RE = re.compile(r"\b[\w'’-]+\b", re.UNICODE)


@dataclass
class ChapterRule:
    name: str
    keywords: List[str]


@dataclass
class AngleRule:
    name: str
    keywords: List[str]


@dataclass
class ClassificationConfig:
    chapters: List[ChapterRule]
    angles: List[AngleRule]
    centrality_keywords: List[str]


@dataclass
class DocumentAnalysis:
    chapter: str
    angles: List[str]
    centrality: str
    keyword_hits: dict


def load_config(path: Path = CONFIG_PATH) -> ClassificationConfig:
    if not path.exists():
        raise SystemExit(f"Config introuvable: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    chapters = [ChapterRule(**entry) for entry in data.get("chapters", [])]
    angles = [AngleRule(**entry) for entry in data.get("angles", [])]
    centrality = data.get("centrality_keywords", [])
    return ClassificationConfig(chapters=chapters, angles=angles, centrality_keywords=centrality)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            title TEXT,
            sha256 TEXT,
            pages INTEGER,
            indexed_at TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS passages (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_analysis (
            document_id INTEGER PRIMARY KEY,
            chapter TEXT,
            angles TEXT,
            centrality TEXT,
            keyword_hits TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts
        USING fts5(content, path, title, content='passages', content_rowid='id');
        """
    )
    conn.commit()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_text_from_pdf(path: Path) -> Tuple[str, int]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return extract_text_with_pdftotext(path)

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        pages.append(page_text)
    text = "\n\n".join(pages)
    return clean_text(text), len(reader.pages)


def extract_text_with_pdftotext(path: Path) -> Tuple[str, int]:
    if not shutil.which("pdftotext"):
        raise SystemExit(
            "Aucun extracteur PDF trouvé. Installez pypdf (pip install -r requirements.txt)"
        )
    result = subprocess.run(
        ["pdftotext", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    text = clean_text(result.stdout)
    pages = text.count("\f") + 1 if text else 0
    return text, pages


def chunk_text(text: str) -> List[str]:
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunk = " ".join(current).strip()
            if chunk:
                chunks.append(chunk)
            current = []
            current_len = 0

    for paragraph in paragraphs:
        if current_len + len(paragraph) + 1 > CHUNK_TARGET and current_len >= CHUNK_MIN:
            flush()
        current.append(paragraph)
        current_len += len(paragraph) + 1
    flush()
    return chunks


def tokenize(text: str) -> List[str]:
    return [token.lower() for token in WORD_RE.findall(text)]


def keyword_score(tokens: List[str], keywords: Iterable[str]) -> int:
    if not tokens:
        return 0
    token_text = " ".join(tokens)
    score = 0
    for keyword in keywords:
        keyword_norm = keyword.lower()
        score += token_text.count(keyword_norm)
    return score


def classify_document(text: str, config: ClassificationConfig) -> DocumentAnalysis:
    tokens = tokenize(text)
    chapter_scores = {
        rule.name: keyword_score(tokens, rule.keywords) for rule in config.chapters
    }
    angles_scores = {rule.name: keyword_score(tokens, rule.keywords) for rule in config.angles}

    chapter = max(chapter_scores, key=chapter_scores.get) if chapter_scores else "non classé"
    if chapter_scores.get(chapter, 0) == 0:
        chapter = "non classé"

    angles = [name for name, score in angles_scores.items() if score > 0]
    if not angles:
        angles = ["non déterminé"]

    central_score = keyword_score(tokens, config.centrality_keywords)
    length = len(tokens)
    density = central_score / max(length, 1)
    if central_score >= 10 or density >= 0.01:
        centrality = "central"
    elif central_score >= 3:
        centrality = "secondaire"
    else:
        centrality = "périphérique"

    return DocumentAnalysis(
        chapter=chapter,
        angles=angles,
        centrality=centrality,
        keyword_hits={"chapters": chapter_scores, "angles": angles_scores},
    )


def upsert_document(conn: sqlite3.Connection, path: Path, title: str, sha: str, pages: int) -> int:
    row = conn.execute("SELECT id, sha256 FROM documents WHERE path = ?", (str(path),)).fetchone()
    indexed_at = dt.datetime.utcnow().isoformat(timespec="seconds")
    if row:
        doc_id, existing_sha = row
        if existing_sha == sha:
            return doc_id
        conn.execute(
            "UPDATE documents SET title = ?, sha256 = ?, pages = ?, indexed_at = ? WHERE id = ?",
            (title, sha, pages, indexed_at, doc_id),
        )
        conn.execute("DELETE FROM passages WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM document_analysis WHERE document_id = ?", (doc_id,))
        conn.execute(
            "DELETE FROM passages_fts WHERE rowid IN (SELECT id FROM passages WHERE document_id = ?)",
            (doc_id,),
        )
        return doc_id

    cursor = conn.execute(
        "INSERT INTO documents (path, title, sha256, pages, indexed_at) VALUES (?, ?, ?, ?, ?)",
        (str(path), title, sha, pages, indexed_at),
    )
    return int(cursor.lastrowid)


def insert_passages(conn: sqlite3.Connection, doc_id: int, title: str, path: Path, chunks: List[str]) -> None:
    for index, content in enumerate(chunks):
        cursor = conn.execute(
            "INSERT INTO passages (document_id, chunk_index, content) VALUES (?, ?, ?)",
            (doc_id, index, content),
        )
        rowid = cursor.lastrowid
        conn.execute(
            "INSERT INTO passages_fts (rowid, content, path, title) VALUES (?, ?, ?, ?)",
            (rowid, content, str(path), title),
        )


def insert_analysis(conn: sqlite3.Connection, doc_id: int, analysis: DocumentAnalysis) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO document_analysis
        (document_id, chapter, angles, centrality, keyword_hits)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            analysis.chapter,
            ", ".join(analysis.angles),
            analysis.centrality,
            json.dumps(analysis.keyword_hits, ensure_ascii=False),
        ),
    )


def index_documents(db_path: Path, documents: List[Path]) -> None:
    config = load_config()
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        for doc in documents:
            if not doc.exists():
                print(f"[!] Fichier introuvable: {doc}")
                continue
            sha = file_sha256(doc)
            title = doc.stem
            text, pages = extract_text_from_pdf(doc)
            chunks = chunk_text(text)
            doc_id = upsert_document(conn, doc, title, sha, pages)
            if not chunks:
                print(f"[!] Aucun texte extrait pour {doc}")
                continue
            insert_passages(conn, doc_id, title, doc, chunks)
            analysis = classify_document(text, config)
            insert_analysis(conn, doc_id, analysis)
            print(f"[+] Indexé {doc} ({len(chunks)} segments)")
        conn.commit()


def query(db_path: Path, query_text: str, limit: int = 5) -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT documents.title,
                   documents.path,
                   snippet(passages_fts, 0, '[', ']', '…', 12) AS snippet,
                   bm25(passages_fts, 1.0, 0.2, 0.2) AS score
            FROM passages_fts
            JOIN passages ON passages_fts.rowid = passages.id
            JOIN documents ON passages.document_id = documents.id
            WHERE passages_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query_text, limit),
        ).fetchall()

    if not rows:
        print("Aucun résultat.")
        return

    for title, path, snippet, score in rows:
        print(f"\n- {title} ({path})")
        print(f"  score: {score:.3f}")
        print(f"  extrait: {snippet}")


def report(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT documents.title,
                   documents.path,
                   document_analysis.chapter,
                   document_analysis.angles,
                   document_analysis.centrality
            FROM documents
            LEFT JOIN document_analysis ON documents.id = document_analysis.document_id
            ORDER BY documents.title
            """,
        ).fetchall()

    for title, path, chapter, angles, centrality in rows:
        print(f"\n- {title}")
        print(f"  chemin: {path}")
        print(f"  chapitre suggéré: {chapter or 'non classé'}")
        print(f"  angles: {angles or 'non déterminé'}")
        print(f"  importance: {centrality or 'inconnu'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index et recherche pour mémoire.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Chemin de la base SQLite.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialiser la base.")

    index_parser = subparsers.add_parser("index", help="Indexer des PDF.")
    index_parser.add_argument("paths", nargs="+", type=Path, help="Fichiers ou dossiers.")

    query_parser = subparsers.add_parser("query", help="Rechercher dans la base.")
    query_parser.add_argument("query", help="Requête FTS (ex: \"autonomie strategique\")")
    query_parser.add_argument("--limit", type=int, default=5)

    subparsers.add_parser("report", help="Afficher un rapport de classification.")

    return parser.parse_args()


def gather_documents(paths: List[Path]) -> List[Path]:
    documents: List[Path] = []
    for path in paths:
        if path.is_dir():
            documents.extend(sorted(path.glob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            documents.append(path)
    return documents


def main() -> None:
    args = parse_args()
    if args.command == "init":
        with sqlite3.connect(args.db) as conn:
            ensure_schema(conn)
        print(f"Base initialisée: {args.db}")
        return

    if args.command == "index":
        documents = gather_documents(args.paths)
        if not documents:
            raise SystemExit("Aucun PDF trouvé.")
        index_documents(args.db, documents)
        return

    if args.command == "query":
        query(args.db, args.query, args.limit)
        return

    if args.command == "report":
        report(args.db)
        return


if __name__ == "__main__":
    main()
