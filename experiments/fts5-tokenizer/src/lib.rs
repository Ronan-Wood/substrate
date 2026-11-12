//! `substrate` — an FTS5 tokenizer for identifier- and path-heavy notes, as a loadable SQLite
//! extension. See README.md. `unsafe` is confined to `ffi`; `split` forbids it.
#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;
pub mod split;
