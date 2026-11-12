/*
** The amalgamation's sqlite3.h carries fts5.h's declarations (fts5_api, fts5_tokenizer, the
** FTS5_TOKENIZE_* flags), and the macOS SDK's copy does too. A separate fts5.h is not shipped in
** the SDK or by Homebrew's sqlite, so this fails loudly if a header without them is picked up.
*/
#ifndef FTS5_MIN_H
#define FTS5_MIN_H
#include <sqlite3.h>
#ifndef _FTS5_H
#error "this sqlite3.h has no FTS5 declarations; point -I at an amalgamation sqlite3.h"
#endif
#endif
