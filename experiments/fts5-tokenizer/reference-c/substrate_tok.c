/*
** substrate_tok — an FTS5 tokenizer for identifier- and path-heavy notes.
**
**   CREATE VIRTUAL TABLE t USING fts5(x, tokenize='substrate');
**   CREATE VIRTUAL TABLE t USING fts5(x, tokenize='substrate remove_diacritics 2');
**
** Arguments are passed straight to the unicode61 tokenizer it wraps, so folding (case, and
** diacritics with remove_diacritics 1 by default) is exactly unicode61's.
**
** WHAT IT DOES. unicode61 already splits snake_case, kebab-case, dotted names and paths at their
** punctuation, and already treats em dashes, curly quotes and arrows as separators. It does NOT:
**   - split camelCase / PascalCase: `IndexStore` is one token, `indexstore`;
**   - index a compound as one term, so a query for `indexstore` cannot find `index_store`;
**   - ignore soft hyphens and zero-width joiners, which split a word in two.
**
** Per maximal run of word characters and joiners (_ - . / \ : U+2010 U+2011):
**   1. split at joiners into SEGMENTS, then each segment at camel boundaries into PARTS;
**   2. fold each part through unicode61;
**   3. emit the parts at consecutive positions (so phrase queries over parts still work, and
**      a plain word produces exactly the token unicode61 would);
**   4. at DOCUMENT time only, emit the separator-free concatenation of the whole run, and of
**      each multi-part segment of a multi-segment run, as FTS5_TOKEN_COLOCATED terms at the
**      position of their first part. Colocated terms do not count toward the column length, so
**      BM25's length normalisation sees only the parts.
**
** WHY THE WHOLE TOKEN IS THE CONCATENATION. The engine lowercases query terms and keeps only
** [A-Za-z0-9_] (substrate/store/fts.py), so `IndexStore` arrives as `indexstore`, and `-./` never
** reach FTS5 at all. A whole token spelled with its separators could never be queried; the
** concatenation can, and it makes `IndexStore`, `index_store` and `index-store` one term.
**
** Camel rules (ASCII only; non-ASCII letters never form a boundary):
**   lower -> Upper                    camelCase   -> camel | Case
**   Upper Upper lower, >=2 on left    HTTPServer  -> HTTP | Server   (IPv6, OAuth stay whole)
**   letters and digits never split    sha256, qwen3, v11 stay whole
*/
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1
#include <stdlib.h>
#include <string.h>

#include "fts5_min.h"

#define MAX_CONCAT 128 /* bytes; a longer compound (a long path) gets no whole-run term */

/* Extra unicode61 token characters for the SCANNER instance only: joiners, then ignorables. */
static const char SCAN_TOKENCHARS[] =
    "_-./\\:"
    "\xE2\x80\x90" /* U+2010 hyphen */
    "\xE2\x80\x91" /* U+2011 non-breaking hyphen */
    "\xC2\xAD"     /* U+00AD soft hyphen */
    "\xE2\x80\x8C" /* U+200C zero-width non-joiner */
    "\xE2\x80\x8D" /* U+200D zero-width joiner */
    "\xE2\x81\xA0" /* U+2060 word joiner */
    ;

typedef struct SubTok {
  fts5_tokenizer api; /* unicode61's methods */
  Fts5Tokenizer *pScan; /* unicode61 + SCAN_TOKENCHARS: finds runs, its output text is unused */
  Fts5Tokenizer *pFold; /* unicode61 with the caller's args: folds each part */
} SubTok;

typedef struct Part {
  int off, len;        /* folded bytes in Run.arena */
  int seg;             /* segment index within the run */
  int iStart, iEnd;    /* byte offsets in the original text */
} Part;

typedef struct Run {
  SubTok *p;
  int flags;
  const char *pText;
  void *pCtx;
  int (*xToken)(void*, int, const char*, int, int, int);
  char *arena; int nArena, capArena;
  Part *parts; int nPart, capPart;
  char *clean; int capClean;
  /* set per folded piece */
  int curSeg, curStart, curEnd;
} Run;

static int grow(void **pp, int *cap, int need, int elem) {
  if (need <= *cap) return SQLITE_OK;
  int n = *cap ? *cap : 64;
  while (n < need) n *= 2;
  void *q = sqlite3_realloc64(*pp, (sqlite3_uint64)n * elem);
  if (!q) return SQLITE_NOMEM;
  *pp = q; *cap = n;
  return SQLITE_OK;
}

/* Joiner at z[0..n): its byte length, else 0. */
static int joinerLen(const unsigned char *z, int n) {
  switch (z[0]) {
    case '_': case '-': case '.': case '/': case '\\': case ':': return 1;
  }
  if (n >= 3 && z[0] == 0xE2 && z[1] == 0x80 && (z[2] == 0x90 || z[2] == 0x91)) return 3;
  return 0;
}

/* Ignorable at z[0..n): its byte length, else 0. */
static int ignorableLen(const unsigned char *z, int n) {
  if (n >= 2 && z[0] == 0xC2 && z[1] == 0xAD) return 2;
  if (n >= 3 && z[0] == 0xE2 && z[1] == 0x80 && (z[2] == 0x8C || z[2] == 0x8D)) return 3;
  if (n >= 3 && z[0] == 0xE2 && z[1] == 0x81 && z[2] == 0xA0) return 3;
  return 0;
}

static int utf8Len(unsigned char c) {
  if (c < 0x80) return 1;
  if ((c & 0xE0) == 0xC0) return 2;
  if ((c & 0xF0) == 0xE0) return 3;
  if ((c & 0xF8) == 0xF0) return 4;
  return 1;
}

#define IS_UP(c) ((c) >= 'A' && (c) <= 'Z')
#define IS_LO(c) ((c) >= 'a' && (c) <= 'z')

static int foldCb(void *pCtx, int tflags, const char *pTok, int nTok, int iS, int iE) {
  Run *r = (Run*)pCtx;
  (void)tflags; (void)iS; (void)iE;
  int rc = grow((void**)&r->arena, &r->capArena, r->nArena + nTok, 1);
  if (rc == SQLITE_OK) rc = grow((void**)&r->parts, &r->capPart, r->nPart + 1, sizeof(Part));
  if (rc != SQLITE_OK) return rc;
  memcpy(r->arena + r->nArena, pTok, nTok);
  Part *pt = &r->parts[r->nPart++];
  pt->off = r->nArena; pt->len = nTok; pt->seg = r->curSeg;
  pt->iStart = r->curStart; pt->iEnd = r->curEnd;
  r->nArena += nTok;
  return SQLITE_OK;
}

static int foldPiece(Run *r, const char *z, int n, int iStart, int iEnd) {
  if (n <= 0) return SQLITE_OK;
  r->curStart = iStart; r->curEnd = iEnd;
  return r->p->api.xTokenize(r->p->pFold, (void*)r, FTS5_TOKENIZE_DOCUMENT, z, n, foldCb);
}

/* One segment [a,b) of pText: strip ignorables, split at camel boundaries, fold each piece. */
static int doSegment(Run *r, int a, int b) {
  const unsigned char *z = (const unsigned char*)r->pText;
  int rc = grow((void**)&r->clean, &r->capClean, b - a + 1, 1);
  if (rc != SQLITE_OK) return rc;
  int n = 0, hadIgn = 0;
  for (int i = a; i < b;) {
    int l = ignorableLen(z + i, b - i);
    if (l) { hadIgn = 1; i += l; continue; }
    r->clean[n++] = (char)z[i++];
  }
  const unsigned char *c = (const unsigned char*)r->clean;
  int start = 0;
  for (int j = 1; j <= n; j++) {
    int cut = 0;
    if (j == n) {
      cut = 1;
    } else if (IS_UP(c[j])) {
      if (IS_LO(c[j-1])) cut = 1;
      else if (IS_UP(c[j-1]) && j + 1 < n && IS_LO(c[j+1]) && j - start >= 2) cut = 1;
    }
    if (!cut) continue;
    int iS = hadIgn ? a : a + start, iE = hadIgn ? b : a + j;
    rc = foldPiece(r, r->clean + start, j - start, iS, iE);
    if (rc != SQLITE_OK) return rc;
    start = j;
  }
  return SQLITE_OK;
}

static int allDigits(Run *r) {
  for (int i = 0; i < r->nArena; i++) {
    if (r->arena[i] < '0' || r->arena[i] > '9') return 0;
  }
  return 1;
}

static int emitConcat(Run *r, int from, int to, int iStart, int iEnd) {
  int len = 0;
  for (int i = from; i < to; i++) len += r->parts[i].len;
  if (len > MAX_CONCAT) return SQLITE_OK;
  char buf[MAX_CONCAT];
  int n = 0;
  for (int i = from; i < to; i++) {
    memcpy(buf + n, r->arena + r->parts[i].off, r->parts[i].len);
    n += r->parts[i].len;
  }
  return r->xToken(r->pCtx, FTS5_TOKEN_COLOCATED, buf, n, iStart, iEnd);
}

static int scanCb(void *pCtx, int tflags, const char *pTok, int nTok, int iStart, int iEnd) {
  Run *r = (Run*)pCtx;
  const unsigned char *z = (const unsigned char*)r->pText;
  int rc = SQLITE_OK;
  (void)tflags; (void)pTok; (void)nTok;
  r->nArena = 0; r->nPart = 0;

  int seg = 0, s = iStart;
  for (int i = iStart; i <= iEnd && rc == SQLITE_OK;) {
    int l = (i < iEnd) ? joinerLen(z + i, iEnd - i) : 0;
    if (i == iEnd || l) {
      if (i > s) {
        int before = r->nPart;
        r->curSeg = seg;
        rc = doSegment(r, s, i);
        if (r->nPart > before) seg++;
      }
      if (i == iEnd) break;
      i += l; s = i;
    } else {
      int k = ignorableLen(z + i, iEnd - i);
      i += k ? k : utf8Len(z[i]);
    }
  }
  if (rc != SQLITE_OK || r->nPart == 0) return rc;

  int doc = (r->flags & FTS5_TOKENIZE_DOCUMENT) && r->nPart > 1 && !allDigits(r);
  int nSeg = r->parts[r->nPart - 1].seg + 1;
  for (int i = 0; i < r->nPart && rc == SQLITE_OK; i++) {
    Part *pt = &r->parts[i];
    rc = r->xToken(r->pCtx, 0, r->arena + pt->off, pt->len, pt->iStart, pt->iEnd);
    if (rc != SQLITE_OK || !doc) continue;
    if (i == 0) rc = emitConcat(r, 0, r->nPart, iStart, iEnd);
    if (rc == SQLITE_OK && nSeg > 1 && (i == 0 || r->parts[i-1].seg != pt->seg)) {
      int j = i;
      while (j < r->nPart && r->parts[j].seg == pt->seg) j++;
      if (j - i > 1) rc = emitConcat(r, i, j, pt->iStart, r->parts[j-1].iEnd);
    }
  }
  return rc;
}

static int subTokenize(Fts5Tokenizer *pTok, void *pCtx, int flags, const char *pText, int nText,
                       int (*xToken)(void*, int, const char*, int, int, int)) {
  SubTok *p = (SubTok*)pTok;
  Run r;
  memset(&r, 0, sizeof(r));
  r.p = p; r.flags = flags; r.pText = pText; r.pCtx = pCtx; r.xToken = xToken;
  int rc = p->api.xTokenize(p->pScan, (void*)&r, flags, pText, nText, scanCb);
  sqlite3_free(r.arena);
  sqlite3_free(r.parts);
  sqlite3_free(r.clean);
  return rc;
}

static void subDelete(Fts5Tokenizer *pTok) {
  SubTok *p = (SubTok*)pTok;
  if (!p) return;
  if (p->pScan) p->api.xDelete(p->pScan);
  if (p->pFold) p->api.xDelete(p->pFold);
  sqlite3_free(p);
}

static int subCreate(void *pUserData, const char **azArg, int nArg, Fts5Tokenizer **ppOut) {
  fts5_api *api = (fts5_api*)pUserData;
  void *u61Data = 0;
  *ppOut = 0;
  SubTok *p = sqlite3_malloc(sizeof(*p));
  if (!p) return SQLITE_NOMEM;
  memset(p, 0, sizeof(*p));
  int rc = api->xFindTokenizer(api, "unicode61", &u61Data, &p->api);
  if (rc == SQLITE_OK) rc = p->api.xCreate(u61Data, azArg, nArg, &p->pFold);
  if (rc == SQLITE_OK) {
    const char **a = sqlite3_malloc64(sizeof(char*) * (nArg + 2));
    if (!a) {
      rc = SQLITE_NOMEM;
    } else {
      for (int i = 0; i < nArg; i++) a[i] = azArg[i];
      a[nArg] = "tokenchars";
      a[nArg + 1] = SCAN_TOKENCHARS;
      rc = p->api.xCreate(u61Data, a, nArg + 2, &p->pScan);
      sqlite3_free(a);
    }
  }
  if (rc != SQLITE_OK) { subDelete((Fts5Tokenizer*)p); return rc; }
  *ppOut = (Fts5Tokenizer*)p;
  return SQLITE_OK;
}

#ifdef _WIN32
__declspec(dllexport)
#endif
int sqlite3_substratetok_init(sqlite3 *db, char **pzErrMsg, const sqlite3_api_routines *pApi) {
  SQLITE_EXTENSION_INIT2(pApi);
  fts5_api *api = 0;
  sqlite3_stmt *st = 0;
  int rc = sqlite3_prepare_v2(db, "SELECT fts5(?1)", -1, &st, 0);
  if (rc == SQLITE_OK) {
    sqlite3_bind_pointer(st, 1, (void*)&api, "fts5_api_ptr", 0);
    sqlite3_step(st);
    rc = sqlite3_finalize(st);
  }
  if (rc != SQLITE_OK || !api || api->iVersion < 2) {
    if (pzErrMsg) *pzErrMsg = sqlite3_mprintf("substrate_tok: FTS5 is not available");
    return SQLITE_ERROR;
  }
  static fts5_tokenizer tok = { subCreate, subDelete, subTokenize };
  return api->xCreateTokenizer(api, "substrate", (void*)api, &tok, 0);
}
