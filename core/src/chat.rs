//! Twitch chat hub: one anonymous IRC connection for every watched channel.
//! stdin lines: `join <channel>` / `part <channel>`.
//! stdout JSON lines: {"c":channel,"u":user,"t":text,"h":hype} and {"ev":"connected"|"disconnected"}.
use std::collections::BTreeSet;
use std::io::{self, BufRead, BufReader, Write};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

type Shared = Arc<Mutex<(BTreeSet<String>, Option<TcpStream>)>>;

pub fn run<I: Iterator<Item = io::Result<String>>>(cmds: I, hype: &str) {
    let hype: Vec<String> = hype.split(',').map(|w| w.to_lowercase()).filter(|w| !w.trim().is_empty()).collect();
    let state: Shared = Arc::new(Mutex::new((BTreeSet::new(), None)));
    let net = state.clone();
    thread::spawn(move || loop {
        connect_and_read(&net, &hype);
        thread::sleep(Duration::from_secs(5));
    });
    for line in cmds.map_while(Result::ok) {
        let mut parts = line.split_whitespace();
        let (Some(cmd), Some(ch)) = (parts.next(), parts.next()) else { continue };
        let ch = ch.trim_start_matches('#').to_lowercase();
        let mut g = state.lock().unwrap();
        let changed = match cmd {
            "join" => g.0.insert(ch.clone()),
            "part" => g.0.remove(&ch),
            _ => false,
        };
        if let (true, Some(sock)) = (changed, g.1.as_mut()) {
            let verb = if cmd == "join" { "JOIN" } else { "PART" };
            let _ = sock.write_all(format!("{verb} #{ch}\r\n").as_bytes());
        }
    }
    // stdin closed: parent exited, so do we.
}

fn emit(v: serde_json::Value) {
    let out = io::stdout();
    let mut out = out.lock();
    let _ = writeln!(out, "{v}");
    let _ = out.flush();
}

fn connect_and_read(state: &Shared, hype: &[String]) {
    let Ok(sock) = TcpStream::connect("irc.chat.twitch.tv:6667") else { return };
    let _ = sock.set_read_timeout(Some(Duration::from_secs(300)));
    let nick = format!("justinfan{}", 10000 + SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.subsec_micros()) % 89999);
    let mut w = match sock.try_clone() { Ok(s) => s, Err(_) => return };
    let mut hello = format!("CAP REQ :twitch.tv/tags\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\n");
    {
        let mut g = state.lock().unwrap();
        for ch in &g.0 {
            hello.push_str(&format!("JOIN #{ch}\r\n"));
        }
        if w.write_all(hello.as_bytes()).is_err() {
            return;
        }
        g.1 = sock.try_clone().ok();
    }
    emit(serde_json::json!({"ev": "connected"}));
    let mut reader = BufReader::new(sock);
    let mut buf = Vec::with_capacity(4096);
    loop {
        buf.clear();
        match reader.read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        let line = String::from_utf8_lossy(&buf);
        let line = line.trim_end();
        if line.starts_with("PING") {
            let _ = w.write_all(b"PONG :tmi.twitch.tv\r\n");
            continue;
        }
        if let Some(m) = parse_privmsg(line) {
            let h = is_hype(&m.2, hype);
            emit(serde_json::json!({"c": m.0, "u": m.1, "t": m.2, "h": h}));
        }
    }
    state.lock().unwrap().1 = None;
    emit(serde_json::json!({"ev": "disconnected"}));
}

/// (channel, display user, text) from a raw IRC line, with or without IRCv3 tags.
pub fn parse_privmsg(line: &str) -> Option<(String, String, String)> {
    let (tags, rest) = match line.strip_prefix('@') {
        Some(r) => r.split_once(' ')?,
        None => ("", line),
    };
    let rest = rest.strip_prefix(':')?;
    let (prefix, rest) = rest.split_once(' ')?;
    let rest = rest.strip_prefix("PRIVMSG #")?;
    let (chan, text) = rest.split_once(" :")?;
    let login = prefix.split('!').next()?.to_string();
    let display = tags
        .split(';')
        .find_map(|kv| kv.strip_prefix("display-name="))
        .filter(|d| !d.is_empty())
        .map(str::to_string)
        .unwrap_or(login);
    Some((chan.to_string(), display, text.to_string()))
}

/// A hype word as a whole word or phrase ("w" matches "W W W", not "wow"). Same rule as gtaclip/chat.py.
pub fn is_hype(text: &str, hype: &[String]) -> bool {
    let low = text.to_lowercase();
    hype.iter().map(|h| h.trim()).filter(|h| !h.is_empty()).any(|h| {
        low.match_indices(h).any(|(i, _)| {
            let before = low[..i].chars().next_back();
            let after = low[i + h.len()..].chars().next();
            !before.is_some_and(char::is_alphanumeric) && !after.is_some_and(char::is_alphanumeric)
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_and_tagged() {
        assert_eq!(
            parse_privmsg(":bob!bob@bob.tmi.twitch.tv PRIVMSG #xqc :CLIP IT"),
            Some(("xqc".into(), "bob".into(), "CLIP IT".into()))
        );
        let tagged = "@badge-info=;color=#FF0000;display-name=BobBy;emotes= :bobby!bobby@bobby.tmi.twitch.tv PRIVMSG #kaicenat :W W W";
        assert_eq!(parse_privmsg(tagged), Some(("kaicenat".into(), "BobBy".into(), "W W W".into())));
        assert_eq!(parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome"), None);
    }

    #[test]
    fn hype_words() {
        let h: Vec<String> = ["clip it", "w ", "kekw"].iter().map(|s| s.to_string()).collect();
        assert!(is_hype("CLIP IT NOW", &h));
        assert!(is_hype("W", &h));
        assert!(!is_hype("wow", &h));
    }
}
