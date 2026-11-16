//! Parallel live checks from public channel pages, so offline streamers never cost a streamlink process.
//! Twitch: the channel page carries `"isLiveBroadcast":true` in its ld+json while live.
//! YouTube: `/@handle/live` carries `"isLiveNow":true` and a canonical watch URL while live.
use std::io::Write;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

const UA: &str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36";

#[derive(Debug, PartialEq)]
pub struct Status {
    pub live: bool,
    pub video_id: String,
}

pub fn run(targets: &[String]) {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(15))
        .user_agent(UA)
        .build();
    let (tx, rx) = mpsc::channel();
    for t in targets {
        let (tx, agent, t) = (tx.clone(), agent.clone(), t.clone());
        thread::spawn(move || {
            let (platform, name) = t.split_once(':').unwrap_or(("twitch", t.as_str()));
            let line = match check(&agent, platform, name) {
                Ok(s) => serde_json::json!({"target": t, "live": s.live, "id": s.video_id}),
                Err(e) => serde_json::json!({"target": t, "error": e}),
            };
            let _ = tx.send(line);
        });
    }
    drop(tx);
    let out = std::io::stdout();
    let mut out = out.lock();
    for line in rx {
        let _ = writeln!(out, "{line}");
    }
}

fn check(agent: &ureq::Agent, platform: &str, name: &str) -> Result<Status, String> {
    let url = page_url(platform, name);
    let body = agent
        .get(&url)
        .set("Accept-Language", "en-US,en;q=0.9")
        .set("Cookie", "CONSENT=YES+1; SOCS=CAI")
        .call()
        .map_err(|e| e.to_string())?
        .into_string()
        .map_err(|e| e.to_string())?;
    Ok(parse(platform, &body))
}

pub fn page_url(platform: &str, name: &str) -> String {
    if platform == "youtube" {
        let h = if name.starts_with('@') || name.starts_with("UC") { name.to_string() } else { format!("@{name}") };
        let base = if h.starts_with("UC") { format!("channel/{h}") } else { h };
        format!("https://www.youtube.com/{base}/live")
    } else {
        format!("https://www.twitch.tv/{}", name.to_lowercase())
    }
}

pub fn parse(platform: &str, body: &str) -> Status {
    if platform == "youtube" {
        let live = body.contains("\"isLiveNow\":true");
        let video_id = if live { canonical_video_id(body).unwrap_or_default() } else { String::new() };
        Status { live, video_id }
    } else {
        Status { live: body.contains("\"isLiveBroadcast\":true"), video_id: String::new() }
    }
}

fn canonical_video_id(body: &str) -> Option<String> {
    let key = "rel=\"canonical\" href=\"https://www.youtube.com/watch?v=";
    let i = body.find(key)? + key.len();
    let id: String = body[i..].chars().take_while(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_').collect();
    (id.len() == 11).then_some(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twitch_live_and_offline() {
        assert!(parse("twitch", r#"{"@type":"VideoObject","publication":{"isLiveBroadcast":true}}"#).live);
        assert!(!parse("twitch", "<html>offline</html>").live);
    }

    #[test]
    fn youtube_live_with_id() {
        let body = r#"<link rel="canonical" href="https://www.youtube.com/watch?v=jfKfPfyJRdk">..."isLiveNow":true"#;
        assert_eq!(parse("youtube", body), Status { live: true, video_id: "jfKfPfyJRdk".into() });
        assert!(!parse("youtube", r#""isLiveNow":false"#).live);
    }

    #[test]
    fn urls() {
        assert_eq!(page_url("twitch", "XQC"), "https://www.twitch.tv/xqc");
        assert_eq!(page_url("youtube", "IShowSpeed"), "https://www.youtube.com/@IShowSpeed/live");
        assert_eq!(page_url("youtube", "UCabc"), "https://www.youtube.com/channel/UCabc/live");
    }
}
