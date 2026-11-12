# Builds the loadable extension. No sqlite3 link: a loadable extension calls SQLite through the
# routine table the host passes to sqlite3_substratetok_init, so the same dylib loads into any
# host whose SQLite was built with FTS5 (Homebrew's Python, the bundled uv CPython).
CC      ?= cc
# NOT the macOS SDK's headers: Apple's sqlite3ext.h defines SQLITE_OMIT_LOAD_EXTENSION, which
# turns every sqlite3_* call into a link against a libsqlite3 the host process may not export.
SQLITE_INC ?= /opt/homebrew/opt/sqlite/include
CFLAGS  ?= -O2 -Wall -Wextra -Werror -mmacosx-version-min=11.0
LIB     := substrate_tok.dylib
PYTHON  ?= python3

$(LIB): substrate_tok.c fts5_min.h
	$(CC) $(CFLAGS) -I$(SQLITE_INC) -dynamiclib -o $@ substrate_tok.c

test: $(LIB)
	$(PYTHON) -I test_substrate_tok.py -v

clean:
	rm -f $(LIB)

.PHONY: test clean
