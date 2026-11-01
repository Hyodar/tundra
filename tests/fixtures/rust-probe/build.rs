use std::process::Command;

fn main() {
    let rustc = std::env::var("RUSTC").unwrap_or_else(|_| "rustc".into());
    let output = Command::new(rustc).arg("-V").output().expect("rustc -V runs");
    let version = String::from_utf8(output.stdout).expect("rustc -V prints UTF-8");
    println!("cargo:rustc-env=PROBE_RUSTC={}", version.trim());
}
