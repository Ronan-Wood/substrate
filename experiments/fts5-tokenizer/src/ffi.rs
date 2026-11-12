//! The FFI boundary: the extension entry point, FTS5's tokenizer vtable, and the two calls into
//! SQLite's own `unicode61`. All of the crate's `unsafe` is in this file. Logic lives in
//! `split.rs`; everything here converts pointers to slices and back.
//!
//! The fts5 structs are declared here rather than taken from bindings. The layout is fts5.h's
//! (fts5_api v2, fts5_tokenizer v1), stable since SQLite 3.20, and this crate uses only the
//! leading members of `fts5_api`.

use std::ffi::{c_char, c_int, c_void, CStr};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;

use libsqlite3_sys as sys;

use crate::split::{Folder, Sink, Splitter, SCAN_TOKENCHARS};

const FTS5_TOKENIZE_DOCUMENT: c_int = 0x0004;
const FTS5_TOKEN_COLOCATED: c_int = 0x0001;

#[repr(C)]
pub struct Fts5Tokenizer {
    _private: [u8; 0],
}

type XToken = unsafe extern "C" fn(*mut c_void, c_int, *const c_char, c_int, c_int, c_int) -> c_int;

#[repr(C)]
#[derive(Clone, Copy)]
struct Fts5TokenizerApi {
    x_create: Option<
        unsafe extern "C" fn(*mut c_void, *mut *const c_char, c_int, *mut *mut Fts5Tokenizer) -> c_int,
    >,
    x_delete: Option<unsafe extern "C" fn(*mut Fts5Tokenizer)>,
    x_tokenize: Option<
        unsafe extern "C" fn(*mut Fts5Tokenizer, *mut c_void, c_int, *const c_char, c_int, Option<XToken>)
            -> c_int,
    >,
}

#[repr(C)]
struct Fts5Api {
    i_version: c_int,
    x_create_tokenizer: Option<
        unsafe extern "C" fn(
            *mut Fts5Api,
            *const c_char,
            *mut c_void,
            *mut Fts5TokenizerApi,
            Option<unsafe extern "C" fn(*mut c_void)>,
        ) -> c_int,
    >,
    x_find_tokenizer: Option<
        unsafe extern "C" fn(*mut Fts5Api, *const c_char, *mut *mut c_void, *mut Fts5TokenizerApi)
            -> c_int,
    >,
}

/// One `substrate` tokenizer instance: two `unicode61` instances and their methods.
struct SubTok {
    api: Fts5TokenizerApi,
    scan: *mut Fts5Tokenizer,
    fold: *mut Fts5Tokenizer,
}

impl Drop for SubTok {
    fn drop(&mut self) {
        if let Some(del) = self.api.x_delete {
            for t in [self.scan, self.fold] {
                if !t.is_null() {
                    // SAFETY: created by this api's xCreate and not yet deleted.
                    unsafe { del(t) };
                }
            }
        }
    }
}

/// Calls one of the wrapped `unicode61` instances over `text`, handing each token to `f`. An
/// error from `f` stops the tokenizer and is returned.
fn run_unicode61<F: FnMut(&[u8], usize, usize) -> Result<(), i32>>(
    api: &Fts5TokenizerApi,
    tok: *mut Fts5Tokenizer,
    flags: c_int,
    text: &[u8],
    mut f: F,
) -> Result<(), i32> {
    unsafe extern "C" fn cb<F: FnMut(&[u8], usize, usize) -> Result<(), i32>>(
        ctx: *mut c_void,
        _tflags: c_int,
        p: *const c_char,
        n: c_int,
        start: c_int,
        end: c_int,
    ) -> c_int {
        let r = catch_unwind(AssertUnwindSafe(|| {
            // SAFETY: ctx is the &mut F passed below, live for the duration of xTokenize.
            let f = unsafe { &mut *(ctx as *mut F) };
            let tok: &[u8] = if n > 0 && !p.is_null() {
                // SAFETY: unicode61 hands a buffer of n bytes valid for this call.
                unsafe { std::slice::from_raw_parts(p as *const u8, n as usize) }
            } else {
                &[]
            };
            f(tok, start.max(0) as usize, end.max(0) as usize)
        }));
        match r {
            Ok(Ok(())) => sys::SQLITE_OK,
            Ok(Err(rc)) => rc,
            Err(_) => sys::SQLITE_ERROR,
        }
    }
    let xt = api.x_tokenize.ok_or(sys::SQLITE_ERROR)?;
    let len = c_int::try_from(text.len()).map_err(|_| sys::SQLITE_TOOBIG)?;
    // SAFETY: tok is a live unicode61 instance; text outlives the call; ctx is &mut f.
    let rc = unsafe {
        xt(tok, &mut f as *mut F as *mut c_void, flags, text.as_ptr() as *const c_char, len, Some(cb::<F>))
    };
    if rc == sys::SQLITE_OK { Ok(()) } else { Err(rc) }
}

struct U61Folder<'a> {
    st: &'a SubTok,
}

impl Folder for U61Folder<'_> {
    fn fold(
        &mut self,
        piece: &[u8],
        out: &mut dyn FnMut(&[u8]) -> Result<(), i32>,
    ) -> Result<(), i32> {
        run_unicode61(&self.st.api, self.st.fold, FTS5_TOKENIZE_DOCUMENT, piece, |t, _, _| out(t))
    }
}

struct FtsSink {
    ctx: *mut c_void,
    x_token: XToken,
}

impl Sink for FtsSink {
    fn emit(&mut self, token: &[u8], colocated: bool, start: usize, end: usize) -> Result<(), i32> {
        let n = c_int::try_from(token.len()).map_err(|_| sys::SQLITE_TOOBIG)?;
        let s = c_int::try_from(start).map_err(|_| sys::SQLITE_TOOBIG)?;
        let e = c_int::try_from(end).map_err(|_| sys::SQLITE_TOOBIG)?;
        let flags = if colocated { FTS5_TOKEN_COLOCATED } else { 0 };
        // SAFETY: x_token and ctx are FTS5's, valid for the enclosing xTokenize call.
        let rc = unsafe { (self.x_token)(self.ctx, flags, token.as_ptr() as *const c_char, n, s, e) };
        if rc == sys::SQLITE_OK { Ok(()) } else { Err(rc) }
    }
}

unsafe extern "C" fn x_create(
    user: *mut c_void,
    az: *mut *const c_char,
    n: c_int,
    out: *mut *mut Fts5Tokenizer,
) -> c_int {
    let r = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: out is FTS5's out-pointer; user is the fts5_api registered in init.
        unsafe { *out = ptr::null_mut() };
        let api = user as *mut Fts5Api;
        let mut st = SubTok {
            api: Fts5TokenizerApi { x_create: None, x_delete: None, x_tokenize: None },
            scan: ptr::null_mut(),
            fold: ptr::null_mut(),
        };
        let mut u61: *mut c_void = ptr::null_mut();
        // SAFETY: api is live for the connection; the name is a NUL-terminated literal.
        let find = match unsafe { (*api).x_find_tokenizer } {
            Some(f) => f,
            None => return sys::SQLITE_ERROR,
        };
        let rc = unsafe { find(api, c"unicode61".as_ptr(), &mut u61, &mut st.api) };
        if rc != sys::SQLITE_OK {
            return rc;
        }
        let Some(create) = st.api.x_create else { return sys::SQLITE_ERROR };
        let n = n.max(0) as usize;
        let mut args: Vec<*const c_char> = if n == 0 || az.is_null() {
            Vec::new()
        } else {
            // SAFETY: FTS5 passes n argument pointers.
            unsafe { std::slice::from_raw_parts(az, n) }.to_vec()
        };
        // The scanner's tokenchars are what define a joiner, so a caller's own tokenchars or
        // separators would make the scanner and the folder disagree about where words end.
        for &a in &args {
            // SAFETY: each arg is a NUL-terminated string FTS5 owns for the duration of xCreate.
            let a = unsafe { CStr::from_ptr(a) }.to_bytes();
            if a.eq_ignore_ascii_case(b"tokenchars") || a.eq_ignore_ascii_case(b"separators") {
                return sys::SQLITE_ERROR;
            }
        }
        // SAFETY: args point at FTS5-owned strings valid during xCreate.
        let rc = unsafe { create(u61, args.as_mut_ptr(), n as c_int, &mut st.fold) };
        if rc != sys::SQLITE_OK {
            return rc;
        }
        let tokenchars = std::ffi::CString::new(SCAN_TOKENCHARS).expect("no NUL in tokenchars");
        args.push(c"tokenchars".as_ptr());
        args.push(tokenchars.as_ptr());
        let rc = unsafe { create(u61, args.as_mut_ptr(), args.len() as c_int, &mut st.scan) };
        if rc != sys::SQLITE_OK {
            return rc;
        }
        unsafe { *out = Box::into_raw(Box::new(st)) as *mut Fts5Tokenizer };
        sys::SQLITE_OK
    }));
    r.unwrap_or(sys::SQLITE_ERROR)
}

unsafe extern "C" fn x_delete(t: *mut Fts5Tokenizer) {
    if !t.is_null() {
        // SAFETY: t came from Box::into_raw in x_create.
        drop(unsafe { Box::from_raw(t as *mut SubTok) });
    }
}

unsafe extern "C" fn x_tokenize(
    t: *mut Fts5Tokenizer,
    ctx: *mut c_void,
    flags: c_int,
    p_text: *const c_char,
    n_text: c_int,
    x_token: Option<XToken>,
) -> c_int {
    let r = catch_unwind(AssertUnwindSafe(|| {
        let Some(x_token) = x_token else { return sys::SQLITE_ERROR };
        // SAFETY: t is a SubTok from x_create; FTS5 passes n_text bytes at p_text.
        let st = unsafe { &*(t as *const SubTok) };
        let text: &[u8] = if n_text > 0 && !p_text.is_null() {
            unsafe { std::slice::from_raw_parts(p_text as *const u8, n_text as usize) }
        } else {
            &[]
        };
        let document = flags & FTS5_TOKENIZE_DOCUMENT != 0;
        let mut folder = U61Folder { st };
        let mut sink = FtsSink { ctx, x_token };
        let mut splitter = Splitter::default();
        // Each run is split inside the scanner's callback, so nothing proportional to the whole
        // text is collected first. The fold is a second, separate unicode61 instance.
        match run_unicode61(&st.api, st.scan, flags, text, |_, s, e| {
            splitter.run(text, s, e, document, &mut folder, &mut sink)
        }) {
            Ok(()) => sys::SQLITE_OK,
            Err(rc) => rc,
        }
    }));
    r.unwrap_or(sys::SQLITE_ERROR)
}

static TOKENIZER: Fts5TokenizerApi = Fts5TokenizerApi {
    x_create: Some(x_create),
    x_delete: Some(x_delete),
    x_tokenize: Some(x_tokenize),
};

/// The extension entry point SQLite derives from `libsubstrate_tok.dylib`.
///
/// # Safety
/// Called by SQLite's extension loader with a live connection and its routine table.
#[no_mangle]
pub unsafe extern "C" fn sqlite3_substratetok_init(
    db: *mut sys::sqlite3,
    pz_err: *mut *mut c_char,
    p_api: *mut sys::sqlite3_api_routines,
) -> c_int {
    let fail = |msg: &CStr| {
        if !pz_err.is_null() {
            // SAFETY: SQLite frees *pz_err with sqlite3_free, so it must come from sqlite3_malloc.
            // (sqlite3_mprintf is variadic and absent from the loadable-extension bindings.)
            let bytes = msg.to_bytes_with_nul();
            unsafe {
                let p = sys::sqlite3_malloc(bytes.len() as c_int) as *mut u8;
                if !p.is_null() {
                    ptr::copy_nonoverlapping(bytes.as_ptr(), p, bytes.len());
                }
                *pz_err = p as *mut c_char;
            }
        }
        sys::SQLITE_ERROR
    };
    let r = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: p_api is the loader's routine table; every sys::sqlite3_* call below uses it.
        if unsafe { sys::rusqlite_extension_init2(p_api) }.is_err() {
            return fail(c"substrate_tok: could not bind the SQLite routine table");
        }
        let mut api: *mut Fts5Api = ptr::null_mut();
        let mut stmt: *mut sys::sqlite3_stmt = ptr::null_mut();
        // SAFETY: standard fts5_api lookup (https://sqlite.org/fts5.html#extending_fts5).
        unsafe {
            if sys::sqlite3_prepare_v2(db, c"SELECT fts5(?1)".as_ptr(), -1, &mut stmt, ptr::null_mut())
                == sys::SQLITE_OK
            {
                sys::sqlite3_bind_pointer(
                    stmt,
                    1,
                    &mut api as *mut *mut Fts5Api as *mut c_void,
                    c"fts5_api_ptr".as_ptr(),
                    None,
                );
                sys::sqlite3_step(stmt);
            }
            sys::sqlite3_finalize(stmt);
        }
        // SAFETY: api, when non-null, is FTS5's global for this connection.
        if api.is_null() || unsafe { (*api).i_version } < 2 {
            return fail(c"substrate_tok: FTS5 is not available");
        }
        let Some(create) = (unsafe { (*api).x_create_tokenizer }) else {
            return fail(c"substrate_tok: fts5_api has no xCreateTokenizer");
        };
        // SAFETY: FTS5 copies the vtable; TOKENIZER is 'static regardless.
        unsafe {
            create(
                api,
                c"substrate".as_ptr(),
                api as *mut c_void,
                &TOKENIZER as *const Fts5TokenizerApi as *mut Fts5TokenizerApi,
                None,
            )
        }
    }));
    r.unwrap_or(sys::SQLITE_ERROR)
}
