fn main() {
    let rustc = env!("PROBE_RUSTC");
    if let Some(version) = rustc.strip_prefix("rustc ")
        && let Some((release, _)) = version.split_once(' ')
    {
        println!("built by rustc {release}");
    }
}
