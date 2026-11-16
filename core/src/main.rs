//! gtacore: fast native helpers for GTA 6 Clipper.
//!
//!   gtacore live twitch:xqc youtube:@IShowSpeed ...   one JSON line per channel (parallel page checks)
//!   gtacore tsdur a.ts b.ts ...                        one JSON line per MPEG-TS segment (duration from PTS)
//!   gtacore chat --hype "clip it,kekw,w "             Twitch chat hub: one IRC socket for every channel.
//!                                                      stdin: "join <ch>" / "part <ch>"; stdout: JSON lines
use std::io::{self, BufRead, Write};

mod chat;
mod live;
mod ts;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        usage();
    };
    let rest = &args[1..];
    match cmd.as_str() {
        "live" => live::run(rest),
        "tsdur" => {
            let out = io::stdout();
            let mut out = out.lock();
            for path in rest {
                let line = match ts::duration_file(path) {
                    Ok(d) => serde_json::json!({"file": path, "duration": (d * 1000.0).round() / 1000.0}),
                    Err(e) => serde_json::json!({"file": path, "error": e.to_string()}),
                };
                let _ = writeln!(out, "{line}");
            }
        }
        "chat" => {
            let hype = rest
                .windows(2)
                .find(|w| w[0] == "--hype")
                .map(|w| w[1].clone())
                .unwrap_or_default();
            chat::run(io::stdin().lock().lines(), &hype);
        }
        "version" | "--version" => println!("gtacore {}", env!("CARGO_PKG_VERSION")),
        _ => usage(),
    }
}

fn usage() -> ! {
    eprintln!("usage: gtacore live <platform:name>... | tsdur <file.ts>... | chat [--hype words] | version");
    std::process::exit(2);
}
