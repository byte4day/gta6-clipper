//! MPEG-TS duration straight from PES timestamps: replaces one ffprobe process per buffer segment.
use std::collections::HashMap;
use std::fs;
use std::io;

const PKT: usize = 188;
const WRAP: i64 = 1 << 33;

#[derive(Default)]
struct Track {
    video: bool,
    prev_raw: Option<i64>,
    last: i64,      // unwrapped PTS of the most recent PES
    pts: Vec<i64>,  // every unwrapped PTS, in arrival order
}

pub fn duration_file(path: &str) -> io::Result<f64> {
    let data = fs::read(path)?;
    duration(&data).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no PES timestamps"))
}

/// Duration in seconds: earliest start to latest end over all elementary streams.
pub fn duration(data: &[u8]) -> Option<f64> {
    let start = (0..PKT.min(data.len())).find(|&o| data[o] == 0x47 && data.get(o + PKT).map_or(true, |b| *b == 0x47))?;
    let mut tracks: HashMap<u16, Track> = HashMap::new();
    let mut off = start;
    while off + PKT <= data.len() {
        let p = &data[off..off + PKT];
        off += PKT;
        if p[0] != 0x47 || p[1] & 0x40 == 0 {
            continue; // lost sync or not a payload start
        }
        let pid = (((p[1] & 0x1f) as u16) << 8) | p[2] as u16;
        let afc = (p[3] >> 4) & 0x3;
        let mut i = 4;
        if afc == 2 {
            continue;
        }
        if afc == 3 {
            i += 1 + p[4] as usize;
        }
        if i + 14 > PKT || p[i..i + 3] != [0, 0, 1] {
            continue;
        }
        let sid = p[i + 3];
        if !(0xC0..=0xEF).contains(&sid) && sid != 0xBD {
            continue; // only audio/video/private streams
        }
        if p[i + 7] & 0x80 == 0 {
            continue; // no PTS
        }
        let b = &p[i + 9..i + 14];
        let pts = (((b[0] as i64 >> 1) & 0x7) << 30)
            | ((b[1] as i64) << 22)
            | (((b[2] as i64) >> 1) << 15)
            | ((b[3] as i64) << 7)
            | ((b[4] as i64) >> 1);
        let t = tracks.entry(pid).or_default();
        t.video |= (0xE0..=0xEF).contains(&sid);
        let un = match t.prev_raw {
            None => pts,
            Some(prev) => {
                let mut d = pts - prev;
                if d < -WRAP / 2 {
                    d += WRAP;
                } else if d > WRAP / 2 {
                    d -= WRAP;
                }
                t.last + d
            }
        };
        t.prev_raw = Some(pts);
        t.last = un;
        t.pts.push(un);
    }
    // Like ffprobe: the video stream decides; audio only when there is no video.
    let has_video = tracks.values().any(|t| t.video);
    let mut lo = i64::MAX;
    let mut hi = i64::MIN;
    for t in tracks.values().filter(|t| t.video == has_video && !t.pts.is_empty()) {
        let mut v = t.pts.clone();
        v.sort_unstable();
        v.dedup();
        let mut steps: Vec<i64> = v.windows(2).map(|w| w[1] - w[0]).collect();
        steps.sort_unstable();
        let frame = steps.get(steps.len() / 2).copied().unwrap_or(0); // B-frames reorder PTS; sorted steps don't care
        lo = lo.min(v[0]);
        hi = hi.max(v[v.len() - 1] + frame);
    }
    (hi > lo).then(|| (hi - lo) as f64 / 90_000.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pes_packet(pid: u16, pts: i64) -> Vec<u8> {
        let mut p = vec![0x47, 0x40 | (pid >> 8) as u8, pid as u8, 0x10];
        p.extend([0, 0, 1, 0xE0, 0, 0, 0x80, 0x80, 5]);
        p.extend([
            0x21 | (((pts >> 30) & 7) << 1) as u8,
            (pts >> 22) as u8,
            ((((pts >> 15) & 0x7f) << 1) | 1) as u8,
            (pts >> 7) as u8,
            (((pts & 0x7f) << 1) | 1) as u8,
        ]);
        p.resize(PKT, 0xff);
        p
    }

    #[test]
    fn four_seconds_at_60fps() {
        let data: Vec<u8> = (0..240).flat_map(|f| pes_packet(256, 1000 + f * 1500)).collect();
        let d = duration(&data).unwrap();
        assert!((d - 4.0).abs() < 1e-6, "{d}");
    }

    #[test]
    fn survives_pts_wrap() {
        let base = WRAP - 90_000; // one second before the 33-bit wrap
        let data: Vec<u8> = (0..120).flat_map(|f| pes_packet(256, (base + f * 1500) % WRAP)).collect();
        let d = duration(&data).unwrap();
        assert!((d - 2.0).abs() < 1e-6, "{d}");
    }

    #[test]
    fn garbage_is_none() {
        assert!(duration(&[0u8; 1000]).is_none());
    }
}
