// The dylib's install name is otherwise the absolute target/ path it was linked at, which leaks
// the build machine's layout into a shipped binary. Nothing loads it by install name (SQLite
// dlopens a path), so @rpath is only a neutral, reproducible value.
fn main() {
    println!("cargo:rustc-cdylib-link-arg=-Wl,-install_name,@rpath/libsubstrate_tok.dylib");
}
