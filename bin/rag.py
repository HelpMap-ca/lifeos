#!/usr/bin/env python3
"""rag — local semantic recall over everything in the LifeOS spine.

The retrieval half the toolkit was missing. Every other tool is stateless: you
paste text in, you get text back. This one answers *from what the owner already
owns* — the vault, 28 archived chats, the tool docs, the HTML deliverables.

  rag index [--force]     embed the spine (incremental by default)
  rag search QUERY        semantic search, ranked
  rag ask QUERY           retrieve, then answer with a local model, with sources
  rag status              what is indexed, with what, when

Design decisions worth knowing:

  · **One vector space across the fleet.** Embeddings use Ollama's
    `qwen3-embedding:0.6b`, which is the same Qwen3-Embedding-0.6B the DGX
    serves as `qwen3-embed`. Verified 2026-07-31: 1024 dims on both, and
    cosine(Mac vector, DGX vector) = 0.9998 on an identical string. So this
    index is readable by the DGX straight off the NAS mirror, and it matches
    the 1024-dim space map.ca is standardising on. `nomic-embed-text` (768)
    would have forked the space three ways for nothing.

  · **Query and document embeddings are NOT symmetric.** Qwen3-Embedding is
    trained with an instruction prefix on the query side only. Documents go in
    raw; queries get the `Instruct:/Query:` wrapper. Skipping this measurably
    degrades retrieval, and it is invisible when it goes wrong.

  · **Vectors are stored pre-normalised**, so search is a plain dot product
    with no per-query normalisation. At this corpus size (hundreds of chunks)
    brute force beats any index, and has no dependencies.

  · stdlib + Ollama HTTP only. No pip, no server, works with the vault locked
    (it just indexes fewer sources).
"""
import argparse
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SPINE = os.path.join(HOME, "LifeOS")
DB = os.path.join(SPINE, "models", "rag.db")
VAULT = "/Volumes/MapAi"
MEMORY = os.path.join(HOME, ".claude", "projects", "-Users-mapai", "memory")

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "qwen3-embedding:0.6b")
CHAT_MODEL = os.environ.get("RAG_CHAT_MODEL", "gemma4:26b-mlx")
DIMS = 1024

# ~400 tokens with ~12% overlap. Chars, not tokens, on purpose: a tokeniser
# would be a dependency, and for prose the ratio is stable enough that the
# retrieval quality difference is nil.
#
# Tuned down from 3200. This corpus is unusually dense — a memory file states a
# dozen unrelated conclusions in as many paragraphs — and at 3200 a single
# embedding averaged so many topics that specific questions missed outright
# ("why did the vault password get silently wiped" ranked an unrelated chat
# above the paragraph that literally answers it). Smaller chunks, sharper
# vectors. The cost is a bigger index, which at this scale is nothing.
CHUNK = 1600
OVERLAP = 200
BATCH = 16

TEXT_EXT = (".md", ".txt")
DATA_EXT = (".json",)
SKIP_DIRS = {".git", "node_modules", "__pycache__", "backups", ".venv"}
# Generated artefacts would index the corpus inside the corpus and grow every run.
# CORPUS.md is `mapai index`'s own file listing — `mapai` classifies it as
# generated too. It is 190 video filenames among other things: it embeds to
# noise, and a chunk of dense filenames tokenises so much worse than prose that
# it crashed the embedding runner outright. Media belongs in filename search.
SKIP_NAMES = {"corpus-index.json", "CORPUS.md", "rag.db", "manifest.json"}

QUERY_INSTRUCT = ("Instruct: Given a question, retrieve passages from the owner's "
                  "personal LifeOS archive that answer it\nQuery: ")


# ----------------------------------------------------------------- sources
def sources():
    """(label, root, extensions) tiers worth embedding.

    Deliberately NOT the whole 74 GB corpus: that is mostly video, which
    embeds to noise. Media stays on filename search in `mapai corpus`.
    """
    out = [
        ("spine", os.path.join(SPINE, "vault"), TEXT_EXT + DATA_EXT),
        ("tools", os.path.join(SPINE, "tools"), TEXT_EXT),
        ("models", os.path.join(SPINE, "models"), TEXT_EXT),
        ("profile", os.path.join(SPINE, "profile"), TEXT_EXT + DATA_EXT),
        # The distilled project knowledge — 20 curated files that state the
        # conclusions the raw chats only imply. Leaving it out was measurable:
        # "what blocked the DGX from reaching the NAS" retrieved nothing useful
        # because "double NAT" appears nowhere else on disk.
        ("memory", MEMORY, TEXT_EXT),
    ]
    if os.path.isdir(VAULT):        # only when the encrypted vault is mounted
        out.append(("deliverables", VAULT, (".html",)))
    return out


def walk(root, exts):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name in SKIP_NAMES or name.startswith("."):
                continue
            if name.lower().endswith(tuple(exts)):
                yield os.path.join(dirpath, name)


def read_text(path):
    """File -> plain text. HTML is stripped; JSON is flattened to readable lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return ""
    low = path.lower()
    if low.endswith(".html"):
        raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        raw = (raw.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                  .replace("&quot;", '"'))
    elif low.endswith(".json"):
        try:
            raw = flatten_json(json.loads(raw))
        except ValueError:
            pass                      # malformed JSON still has readable text
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", raw)).strip()


def flatten_json(obj, prefix=""):
    """JSON -> 'key: value' lines. Keeps field names, which carry real meaning
    in this corpus (house-dna, secrets manifest, reviews)."""
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("_"):
                continue
            lines.append(flatten_json(v, (prefix + "." + str(k)) if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:200]):
            lines.append(flatten_json(v, "%s[%d]" % (prefix, i)))
    else:
        s = str(obj)
        if s and s.lower() not in ("none", "null", ""):
            return "%s: %s" % (prefix, s) if prefix else s
        return ""
    return "\n".join(x for x in lines if x)


def chunk_text(text):
    """Chunk on semantic boundaries: headings, then paragraphs.

    The atomic unit is a paragraph, not a character window. These documents put
    one self-contained fact per paragraph, so cutting mid-paragraph splits a
    conclusion from its reason and both halves embed poorly.
    """
    if not text:
        return []
    # Headings first — a markdown '#' line, or a whole-line bold label, which is
    # how the memory files and audit notes actually mark their sections.
    blocks, cur = [], []
    for line in text.split("\n"):
        heading = re.match(r"^#{1,6}\s+\S", line) or re.match(r"^\*\*[^*]{3,}\*\*", line.strip())
        if heading and cur:
            blocks.append("\n".join(cur)); cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    # Pack paragraphs into chunks WITHIN a block, never across blocks. Packing
    # across them silently undoes the split above: the `hdiutil chpass` warning
    # got merged into a chunk about the NAS mirror, and its vector was then
    # dominated by the unrelated topic, so the paragraph that answers the
    # question ranked below chunks that merely share vocabulary with it.
    chunks = []
    for b in blocks:
        units = []
        for para in re.split(r"\n\s*\n", b):
            para = para.strip()
            if not para:
                continue
            while len(para) > CHUNK:      # a single oversized paragraph
                units.append(para[:CHUNK])
                para = para[CHUNK - OVERLAP:]
            if para:
                units.append(para)
        buf = ""
        for u in units:
            if not buf:
                buf = u
            elif len(buf) + len(u) + 2 <= CHUNK:
                buf = buf + "\n\n" + u
            else:
                chunks.append(buf)
                # Carry a tail so a fact split across the boundary stays
                # retrievable from either side.
                buf = ((buf[-OVERLAP:] + "\n\n" + u) if len(buf) > OVERLAP else u)
        if buf.strip():
            chunks.append(buf)
    return [c.strip() for c in chunks if len(c.strip()) > 40]


# --------------------------------------------------------------- embedding
def embed(texts, tries=3):
    """Embed a batch via Ollama. Returns a list of normalised float lists."""
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(OLLAMA + "/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read()).get("embeddings") or []
            if len(out) != len(texts):
                raise ValueError("got %d embeddings for %d inputs" % (len(out), len(texts)))
            return [normalise(v) for v in out]
        except urllib.error.HTTPError as exc:
            # Surface what Ollama actually objected to. A bare "HTTP 400" sent
            # me looking at the wrong thing entirely.
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            last = "HTTP %s %s" % (exc.code, detail)
            break                     # a 400 is deterministic; retrying is pointless
        except Exception as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise EmbedError("embed failed: %s" % last)


class EmbedError(RuntimeError):
    pass


def embed_chunks(pieces, depth=0):
    """Embed chunks, surviving pathological ones. Returns [(text, vec), ...].

    A char-based chunk size is not a token budget: dense non-prose (file paths,
    hashes, base64) can be several times more tokens per character, and a batch
    of it can take the embedding runner down. Rather than lose the whole file,
    fall back to one-at-a-time, then bisect the offender. Anything that still
    will not embed at the floor size is reported, never silently dropped.
    """
    out = []
    for i in range(0, len(pieces), BATCH):
        batch = pieces[i:i + BATCH]
        try:
            out.extend(zip(batch, embed(batch)))
            continue
        except EmbedError:
            pass
        for piece in batch:           # isolate which member of the batch is bad
            try:
                out.extend(zip([piece], embed([piece])))
            except EmbedError:
                if len(piece) < 400 or depth > 4:
                    raise EmbedError("irreducible chunk (%d chars): %r"
                                     % (len(piece), piece[:80]))
                half = len(piece) // 2
                out.extend(embed_chunks([piece[:half], piece[half:]], depth + 1))
    return out


def normalise(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def pack(vec):
    return struct.pack("<%df" % len(vec), *vec)


def unpack(blob):
    return struct.unpack("<%df" % (len(blob) // 4), blob)


# ---------------------------------------------------------------- storage
def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    cx = sqlite3.connect(DB)
    cx.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY, path TEXT, rel TEXT, source TEXT,
        ord INTEGER, text TEXT, mtime REAL, size INTEGER, vec BLOB)""")
    cx.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
    cx.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    # Lexical half of hybrid search. FTS5 + bm25() ship with sqlite, so this
    # costs nothing and fixes what dense retrieval alone is bad at: rare exact
    # terms. Kept in sync by rebuilding after each index run rather than with
    # triggers — simpler, and indexing is the only writer.
    cx.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
               "text, content='chunks', content_rowid='id')")
    return cx


def rebuild_fts(cx):
    cx.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    cx.commit()


def meta_set(cx, k, v):
    cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


def meta_get(cx, k, d=None):
    row = cx.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else d


# ------------------------------------------------------------------ index
def cmd_index(args):
    cx = connect()
    if args.force:
        cx.execute("DELETE FROM chunks"); cx.commit()
        print("cleared existing index")

    # A file is re-embedded only when mtime or size moved.
    known = {}
    for path, mt, sz in cx.execute("SELECT path, mtime, size FROM chunks GROUP BY path"):
        known[path] = (mt, sz)

    todo, seen, skipped = [], set(), 0
    for label, root, exts in sources():
        for path in walk(root, exts):
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen.add(path)
            if known.get(path) == (st.st_mtime, st.st_size):
                skipped += 1
                continue
            todo.append((label, root, path, st))

    # Files that vanished should not linger in the index.
    gone = [p for p in known if p not in seen]
    for p in gone:
        cx.execute("DELETE FROM chunks WHERE path=?", (p,))
    if gone:
        cx.commit()
        print("dropped %d file(s) no longer on disk" % len(gone))

    if not todo:
        print("index up to date — %d file(s) unchanged" % skipped)
        return summarise(cx)

    print("embedding %d changed file(s) with %s (%d unchanged)"
          % (len(todo), EMBED_MODEL, skipped))
    total_chunks, done_files, failed, t0 = 0, 0, [], time.time()

    # One file = one transaction. Committing mid-file would leave it partially
    # indexed yet recorded as known, and the next incremental run would skip
    # the missing half forever.
    for label, root, path, st in todo:
        pieces = chunk_text(read_text(path))
        if not pieces:
            continue
        rel = os.path.relpath(path, root)
        try:
            embedded = embed_chunks(pieces)
        except EmbedError as exc:
            failed.append((rel, str(exc)))
            print("  !! %s — %s" % (rel, exc))
            continue
        cx.execute("DELETE FROM chunks WHERE path=?", (path,))
        cx.executemany(
            "INSERT INTO chunks(path,rel,source,ord,text,mtime,size,vec)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(path, rel, label, i, piece, st.st_mtime, st.st_size, pack(v))
             for i, (piece, v) in enumerate(embedded)])
        cx.commit()
        total_chunks += len(embedded)
        done_files += 1
        if args.verbose:
            print("  %-52s %3d chunks" % (rel[:52], len(pieces)))

    meta_set(cx, "model", EMBED_MODEL)
    meta_set(cx, "dims", DIMS)
    meta_set(cx, "built", time.strftime("%Y-%m-%dT%H:%M:%S"))
    cx.commit()
    rebuild_fts(cx)
    print("indexed %d chunks from %d file(s) in %.1fs"
          % (total_chunks, done_files, time.time() - t0))
    if failed:
        # Never let a partial index look complete.
        print("\n%d file(s) FAILED and are not in the index:" % len(failed))
        for rel, why in failed:
            print("   %-50s %s" % (rel[:50], why))
    summarise(cx)


def summarise(cx):
    n = cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    f = cx.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
    print("index: %d chunks · %d files · %s" % (n, f, DB))
    for src, c, fc in cx.execute(
            "SELECT source, COUNT(*), COUNT(DISTINCT path) FROM chunks"
            " GROUP BY source ORDER BY 2 DESC"):
        print("   %-13s %5d chunks  %4d files" % (src, c, fc))


# ----------------------------------------------------------------- search
def fts_ranks(cx, query, limit=50):
    """Lexical ranking via BM25. Returns {chunk_id: rank}, best rank = 0."""
    # Feed FTS5 bare terms only: raw user text can contain operators (quotes,
    # NEAR, '*') that make it throw a syntax error on an ordinary question.
    terms = [t for t in re.findall(r"[A-Za-z0-9_]{2,}", query)]
    if not terms:
        return {}
    match = " OR ".join('"%s"' % t for t in terms)
    try:
        rows = cx.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts) LIMIT ?", (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: i for i, r in enumerate(rows)}


def search(cx, query, k=6, per_file=2):
    """Hybrid retrieval: dense cosine fused with BM25 by reciprocal rank.

    Dense alone was not enough here. Qwen3-Embedding puts this corpus in a
    narrow similarity band (~0.62-0.69), so the paragraph that literally
    answers a question can sit 8th behind chunks that merely share its
    vocabulary. RRF needs no score normalisation between the two very
    different scales, and a chunk strong on either signal surfaces.
    """
    qv = embed([QUERY_INSTRUCT + query])[0]
    rows = cx.execute("SELECT id, rel, source, path, ord, text, vec FROM chunks").fetchall()
    if not rows:
        return []
    dense = sorted(
        ((sum(a * b for a, b in zip(qv, unpack(r[6]))), r) for r in rows),
        key=lambda x: -x[0])
    lex = fts_ranks(cx, query)

    K = 60.0                       # standard RRF damping
    fused = {}
    for rank, (score, r) in enumerate(dense):
        fused[r[0]] = [1.0 / (K + rank), score, r]
    for cid, rank in lex.items():
        if cid in fused:
            fused[cid][0] += 1.0 / (K + rank)
    order = sorted(fused.values(), key=lambda x: -x[0])

    out, used = [], {}
    for _fs, cos, r in order:
        _id, rel, source, path, ordn, text, _blob = r
        if used.get(path, 0) >= per_file:         # spread hits across documents
            continue
        used[path] = used.get(path, 0) + 1
        out.append((cos, rel, source, path, ordn, text))
        if len(out) >= k:
            break
    return out


def cmd_search(args):
    cx = connect()
    hits = search(cx, args.query, args.k)
    if not hits:
        return print("nothing indexed yet — run:  rag index")
    for score, rel, source, path, ordn, text in hits:
        snippet = re.sub(r"\s+", " ", text)[:240]
        print("\n\033[1m%.3f  %s\033[0m  \033[2m[%s #%d]\033[0m" % (score, rel, source, ordn))
        print("   " + snippet + ("…" if len(text) > 240 else ""))


# -------------------------------------------------------------------- ask
def cmd_ask(args):
    cx = connect()
    hits = search(cx, args.query, args.k)
    if not hits:
        return print("nothing indexed yet — run:  rag index")

    ctx = "\n\n".join("[%d] %s\n%s" % (i + 1, h[1], h[5]) for i, h in enumerate(hits))
    sys_p = ("You answer strictly from the supplied context, which comes from the owner's "
             "own LifeOS archive. Cite the bracketed source numbers inline like [1]. "
             "If the context does not contain the answer, say so plainly — do not "
             "guess and do not use outside knowledge.")
    body = json.dumps({
        "model": args.model, "stream": True,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": "Context:\n%s\n\nQuestion: %s" % (ctx, args.query)}],
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in r:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sys.stdout.write(d.get("message", {}).get("content", ""))
                sys.stdout.flush()
                if d.get("done"):
                    break
    except urllib.error.HTTPError as exc:
        raise SystemExit("\nchat failed: HTTP %s — is %s pulled?" % (exc.code, args.model))
    print("\n\n\033[2msources:\033[0m")
    for i, h in enumerate(hits):
        print("  [%d] %s  \033[2m(%s, score %.3f)\033[0m" % (i + 1, h[1], h[2], h[0]))


def cmd_status(args):
    cx = connect()
    n = cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not n:
        return print("no index yet — run:  rag index")
    print("model : %s (%s dims)" % (meta_get(cx, "model", "?"), meta_get(cx, "dims", "?")))
    print("built : %s" % meta_get(cx, "built", "?"))
    print("size  : %.1f MB" % (os.path.getsize(DB) / 1e6))
    summarise(cx)


def main():
    ap = argparse.ArgumentParser(prog="rag", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("index", help="embed the spine (incremental)")
    p.add_argument("--force", action="store_true", help="rebuild from scratch")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("search", help="semantic search")
    p.add_argument("query", nargs="+"); p.add_argument("-k", type=int, default=6)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("ask", help="retrieve, then answer locally with sources")
    p.add_argument("query", nargs="+"); p.add_argument("-k", type=int, default=6)
    p.add_argument("--model", default=CHAT_MODEL)
    p.set_defaults(fn=cmd_ask)
    sub.add_parser("status", help="what is indexed").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        return ap.print_help()
    if isinstance(getattr(args, "query", None), list):
        args.query = " ".join(args.query)
    args.fn(args)


if __name__ == "__main__":
    main()
