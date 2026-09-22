//! Imports OTB games from PGN files into the masters database
//! (`PUT /import/masters`), like `import-master.py`, but fast enough for
//! multi-million game collections.
//!
//! Games are skipped client-side unless they have both ratings, an average
//! rating of at least `--min-avg-rating` (the server rejects anything below
//! 2200), a decisive or drawn result, a year from 1952 on, and start from the
//! standard position. Game ids are derived from the game content, so importing the
//! same file twice only yields duplicates.

use std::{
    ffi::OsStr,
    fs::File,
    io,
    ops::ControlFlow,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
    thread,
};

use clap::Parser;
use indicatif::{ProgressBar, ProgressDrawTarget, ProgressStyle};
use pgn_reader::{RawTag, Reader, SanPlus, Visitor};
use serde::Serialize;
use shakmaty::{CastlingMode, Chess, Color, Position};

/// The server does not accept masters games before this year (see `Year` in
/// `src/model/date.rs`).
const MIN_YEAR: u16 = 1952;

#[derive(Serialize)]
struct Player {
    name: String,
    rating: u16,
}

#[derive(Serialize)]
struct MastersGame {
    id: String,
    event: String,
    site: String,
    date: String,
    round: String,
    white: Player,
    black: Player,
    winner: Option<&'static str>,
    moves: String,
}

#[derive(Default)]
struct Tags {
    event: Option<String>,
    site: Option<String>,
    date: Option<String>,
    round: Option<String>,
    white: Option<String>,
    black: Option<String>,
    white_elo: Option<u16>,
    black_elo: Option<u16>,
    result: Option<Option<Color>>,
    non_standard: bool,
}

struct Movetext {
    tags: Tags,
    pos: Chess,
    moves: Vec<String>,
}

struct Importer {
    tx: crossbeam::channel::Sender<MastersGame>,
    min_avg_rating: u16,
    skipped: u64,
}

fn utf8(value: RawTag<'_>) -> String {
    value.decode_utf8_lossy().trim().to_owned()
}

fn rating(value: RawTag<'_>) -> Option<u16> {
    btoi::btou(value.as_bytes()).ok().filter(|&r| r > 0)
}

impl Visitor for Importer {
    type Tags = Tags;
    type Movetext = Movetext;
    type Output = ();

    fn begin_tags(&mut self) -> ControlFlow<Self::Output, Self::Tags> {
        ControlFlow::Continue(Tags::default())
    }

    fn tag(&mut self, tags: &mut Tags, name: &[u8], value: RawTag<'_>) -> ControlFlow<()> {
        match name {
            b"Event" => tags.event = Some(utf8(value)),
            b"Site" => tags.site = Some(utf8(value)),
            b"Date" => tags.date = Some(utf8(value)),
            b"Round" => tags.round = Some(utf8(value)),
            b"White" => tags.white = Some(utf8(value)),
            b"Black" => tags.black = Some(utf8(value)),
            b"WhiteElo" => tags.white_elo = rating(value),
            b"BlackElo" => tags.black_elo = rating(value),
            b"Result" => {
                tags.result = match value.as_bytes() {
                    b"1-0" => Some(Some(Color::White)),
                    b"0-1" => Some(Some(Color::Black)),
                    b"1/2-1/2" => Some(None),
                    _ => None,
                }
            }
            b"FEN" => {
                tags.non_standard |= value.as_bytes()
                    != b"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            }
            b"Variant" => {
                let v = value.as_bytes().to_ascii_lowercase();
                tags.non_standard |= v != b"standard" && v != b"chess";
            }
            _ => (),
        }
        ControlFlow::Continue(())
    }

    fn begin_movetext(&mut self, tags: Tags) -> ControlFlow<(), Movetext> {
        let accept = match (tags.white_elo, tags.black_elo) {
            (Some(w), Some(b)) => (u32::from(w) + u32::from(b)) / 2 >= u32::from(self.min_avg_rating),
            _ => false,
        } && tags.result.is_some()
            && !tags.non_standard
            && tags.white.as_deref().is_some_and(|n| !n.is_empty() && n != "?")
            && tags.black.as_deref().is_some_and(|n| !n.is_empty() && n != "?")
            && tags
                .date
                .as_deref()
                .and_then(|d| d.get(..4))
                .and_then(|y| y.parse::<u16>().ok())
                .is_some_and(|y| y >= MIN_YEAR);
        if !accept {
            self.skipped += 1;
            return ControlFlow::Break(());
        }
        ControlFlow::Continue(Movetext {
            tags,
            pos: Chess::default(),
            moves: Vec::with_capacity(100),
        })
    }

    fn san(&mut self, movetext: &mut Movetext, san_plus: SanPlus) -> ControlFlow<()> {
        match san_plus.san.to_move(&movetext.pos) {
            Ok(m) => {
                movetext.moves.push(m.to_uci(CastlingMode::Standard).to_string());
                movetext.pos.play_unchecked(m);
                ControlFlow::Continue(())
            }
            Err(_) => {
                self.skipped += 1;
                ControlFlow::Break(())
            }
        }
    }

    fn end_game(&mut self, movetext: Movetext) {
        if movetext.moves.is_empty() {
            self.skipped += 1;
            return;
        }
        let t = movetext.tags;
        let mut game = MastersGame {
            id: String::new(),
            event: t.event.unwrap_or_else(|| "?".to_owned()),
            site: t.site.unwrap_or_else(|| "?".to_owned()),
            date: t.date.unwrap_or_default(),
            round: t.round.unwrap_or_else(|| "?".to_owned()),
            white: Player {
                name: t.white.unwrap_or_default(),
                rating: t.white_elo.unwrap_or_default(),
            },
            black: Player {
                name: t.black.unwrap_or_default(),
                rating: t.black_elo.unwrap_or_default(),
            },
            winner: match t.result.flatten() {
                Some(Color::White) => Some("white"),
                Some(Color::Black) => Some("black"),
                None => None,
            },
            moves: movetext.moves.join(" "),
        };
        game.id = deterministic_id(&game);
        self.tx.send(game).expect("send game");
    }
}

/// Stable 8 character base62 id from the game content (FNV-1a, 64 bit), so
/// that re-imports of the same game are recognized as duplicates.
fn deterministic_id(game: &MastersGame) -> String {
    const ALPHABET: &[u8; 62] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for part in [
        &game.white.name,
        &game.black.name,
        &game.date,
        &game.event,
        &game.round,
        &game.moves,
    ] {
        for &b in part.as_bytes().iter().chain(b"\x1f") {
            hash ^= u64::from(b);
            hash = hash.wrapping_mul(0x0100_0000_01b3);
        }
    }
    (0..8)
        .map(|_| {
            let c = ALPHABET[(hash % 62) as usize] as char;
            hash /= 62;
            c
        })
        .collect()
}

#[derive(Parser)]
struct Args {
    #[arg(long, default_value = "http://localhost:9002")]
    endpoint: String,
    /// The server rejects games below an average rating of 2200.
    #[arg(long, default_value = "2200")]
    min_avg_rating: u16,
    /// Parallel HTTP requests.
    #[arg(long, default_value = "4")]
    workers: usize,
    pgns: Vec<PathBuf>,
}

#[derive(Default)]
struct Counts {
    imported: AtomicU64,
    duplicate: AtomicU64,
    rejected: AtomicU64,
}

fn main() -> Result<(), io::Error> {
    let args = Args::parse();
    let counts: &'static Counts = Box::leak(Box::default());

    let (tx, rx) = crossbeam::channel::bounded::<MastersGame>(1000);

    let workers: Vec<_> = (0..args.workers)
        .map(|_| {
            let rx = rx.clone();
            let url = format!("{}/import/masters", args.endpoint);
            thread::spawn(move || {
                let client = reqwest::blocking::Client::builder()
                    .timeout(None)
                    .build()
                    .expect("client");
                while let Ok(game) = rx.recv() {
                    let res = client.put(&url).json(&game).send().expect("send game");
                    if res.status().is_success() {
                        counts.imported.fetch_add(1, Ordering::Relaxed);
                    } else {
                        let text = res.text().unwrap_or_default();
                        if text.contains("duplicate") {
                            counts.duplicate.fetch_add(1, Ordering::Relaxed);
                        } else {
                            counts.rejected.fetch_add(1, Ordering::Relaxed);
                            eprintln!("{} ({} - {}): {}", game.id, game.white.name, game.black.name, text);
                        }
                    }
                }
            })
        })
        .collect();
    drop(rx);

    let mut skipped = 0;
    for arg in args.pgns {
        let file = File::open(&arg)?;
        let progress = ProgressBar::with_draw_target(
            Some(file.metadata()?.len()),
            ProgressDrawTarget::stdout_with_hz(1),
        )
        .with_style(
            ProgressStyle::with_template("{prefix} {wide_bar} {bytes_per_sec:>14} {eta:>7}").unwrap(),
        )
        .with_prefix(format!("{arg:?}"));
        let file = progress.wrap_read(file);

        let uncompressed: Box<dyn io::Read> = if arg.extension() == Some(OsStr::new("bz2")) {
            Box::new(bzip2::read::MultiBzDecoder::new(file))
        } else if arg.extension() == Some(OsStr::new("zst")) {
            Box::new(zstd::Decoder::new(file)?)
        } else {
            Box::new(file)
        };

        let mut importer = Importer {
            tx: tx.clone(),
            min_avg_rating: args.min_avg_rating,
            skipped: 0,
        };
        Reader::new(uncompressed).visit_all_games(&mut importer)?;
        skipped += importer.skipped;
        progress.finish();
    }

    drop(tx);
    for w in workers {
        w.join().expect("worker join");
    }

    println!(
        "imported: {}, duplicate: {}, rejected: {}, skipped (filter/illegal): {}",
        counts.imported.load(Ordering::Relaxed),
        counts.duplicate.load(Ordering::Relaxed),
        counts.rejected.load(Ordering::Relaxed),
        skipped
    );
    Ok(())
}
